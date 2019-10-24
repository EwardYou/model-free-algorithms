import tensorflow as tf
from tensorflow.contrib.layers import layer_norm

from basic_model.model import Module
from utility.tf_distributions import DiagGaussian


class SoftPolicy(Module):
    """ Interface """
    def __init__(self,
                 name,
                 args,
                 graph,
                 state,
                 next_state,
                 action_dim,
                 scope_prefix='',
                 log_tensorboard=False,
                 log_params=False):
        self.state = state
        self.next_state = next_state
        self.action_dim = action_dim
        self.norm = layer_norm if 'layernorm' in args and args['layernorm'] else None
        self.noisy_sigma = args['noisy_sigma']
        self.has_target_net = args['target']
        self.LOG_STD_MIN = -20.
        self.LOG_STD_MAX = 2.

        super().__init__(name, 
                         args, 
                         graph, 
                         scope_prefix=scope_prefix,
                         log_tensorboard=log_tensorboard, 
                         log_params=log_params)

    @property
    def main_variables(self):
        return self.graph.get_collection(name=tf.GraphKeys.TRAINABLE_VARIABLES, scope=self.variable_scope + '/main')
    
    @property
    def target_variables(self):
        return self.graph.get_collection(name=tf.GraphKeys.TRAINABLE_VARIABLES, scope=self.variable_scope + '/target')

    def _build_graph(self):
        self.action_det, self.action, self.logpi = self._build_policy(self.state, 'main', False)
        if self.has_target_net:
            _, self.next_action, self.next_logpi = self._build_policy(self.next_state, 'target', False)
        else:
            _, self.next_action, self.next_logpi = self._build_policy(self.next_state, 'main', True)

        self.init_target_op, self.update_target_op = self._target_net_ops()

    def _build_policy(self, state, name, reuse):
        def stochastic_policy_net(state, units, action_dim, norm, name='policy_net'):
            noisy_norm_activation = lambda x, u, norm: self.noisy_norm_activation(x, u, norm=norm, sigma=self.args['noisy_sigma'])
            x = state
            self.reset_counter('noisy')     # reset noisy counter for each call to enable reuse if desirable

            with tf.variable_scope(name):
                for i, u in enumerate(units):
                    layer = self.dense_norm_activation if i < len(units) - self.args['n_noisy']  else noisy_norm_activation
                    x = layer(x, u, norm=norm)

                mean = self.dense(x, action_dim, name='action_mean')

                # constrain logstd to be in range [LOG_STD_MIN, LOG_STD_MAX]
                logstd = self.dense(x, action_dim)
                logstd = tf.tanh(logstd, name='action_logstd')
                logstd = self.LOG_STD_MIN + .5 * (self.LOG_STD_MAX-self.LOG_STD_MIN) * (logstd + 1)
                # logstd = tf.clip_by_value(logstd, self.LOG_STD_MIN, self.LOG_STD_MAX)

            return mean, logstd, mean

        def squash_correction(action, logpi):
            """ squash action in range [-1, 1] """
            with tf.name_scope('squash'):
                action_new = tf.tanh(action)
                sub = 2 * tf.reduce_sum(tf.log(2.) + action - tf.nn.softplus(2 * action), axis=1, keepdims=True)
                logpi -= sub

            return action_new, logpi

        """ Function Body """
        with tf.variable_scope(name, reuse=reuse):
            mean, logstd, action_det = stochastic_policy_net(state, 
                                                            self.args['units'], 
                                                            self.action_dim, 
                                                            self.norm)

            action_distribution = DiagGaussian((mean, logstd))

            orig_action = action_distribution.sample()
            orig_logpi = action_distribution.logp(orig_action)

            # Enforcing action bound
            action, logpi = squash_correction(orig_action, orig_logpi)
            
        return action_det, action, logpi

    def _target_net_ops(self):
        if not self.has_target_net:
            return [], []
        with tf.name_scope('target_net_op'):
            target_main_var_pairs = list(zip(self.target_variables, self.main_variables))
            init_target_op = list(map(lambda v: tf.assign(v[0], v[1], name='init_target_op'), target_main_var_pairs))
            update_target_op = list(map(lambda v: tf.assign(v[0], self.polyak * v[0] + (1. - self.polyak) * v[1], name='update_target_op'), target_main_var_pairs))

        return init_target_op, update_target_op  


class SoftQ(Module):
    """ Interface """
    def __init__(self, 
                 name, 
                 args, 
                 graph,
                 state,
                 next_state,
                 stored_action_repr,
                 action_repr,
                 next_action_repr,
                 scope_prefix='',
                 log_tensorboard=False,
                 log_params=False):
        self.state = state
        self.next_state = next_state
        self._stored_action_repr = stored_action_repr
        self.action_repr = action_repr
        self.next_action_repr = next_action_repr
        self.norm = layer_norm if 'layernorm' in args and args['layernorm'] else None
        self.polyak = args['polyak']

        super().__init__(name, 
                         args, 
                         graph, 
                         scope_prefix=scope_prefix,
                         log_tensorboard=log_tensorboard,
                         log_params=log_params)

    @property
    def main_variables(self):
        return self.graph.get_collection(name=tf.GraphKeys.TRAINABLE_VARIABLES, scope=self.variable_scope + '/main')
    
    @property
    def target_variables(self):
        return self.graph.get_collection(name=tf.GraphKeys.TRAINABLE_VARIABLES, scope=self.variable_scope + '/target')

    """ Implementation """
    def _build_graph(self):
        def Q_net(state, action, reuse, name):
            x = state
            with tf.variable_scope(name, reuse=reuse):
                for i, u in enumerate(self.args['units']):
                    if i < 2:
                        x = tf.concat([x, action], 1)
                    x = self.dense_norm_activation(x, u, norm=self.norm)

                x = self.dense(x, 1, name='Q')

            return x

        """ Function Body """
        # online network
        with tf.variable_scope('main'):
            self.Q1 = Q_net(self.state, self._stored_action_repr, False, 'Qnet1')
            self.Q2 = Q_net(self.state, self._stored_action_repr, False, 'Qnet2')
            self.Q1_with_actor = Q_net(self.state, self.action_repr, True, 'Qnet1')
            self.Q2_with_actor = Q_net(self.state, self.action_repr, True, 'Qnet2')
            self.Q = tf.minimum(self.Q1, self.Q2, 'Q')
            self.Q_with_actor = tf.minimum(self.Q1_with_actor, self.Q2_with_actor, 'Q_with_actor')

        # target network
        with tf.variable_scope('target'):
            self.next_Q1_with_actor = Q_net(self.next_state, self.next_action_repr, False, 'Qnet1_target')
            self.next_Q2_with_actor = Q_net(self.next_state, self.next_action_repr, False, 'Qnet2_target')
            self.next_Q_with_actor = tf.minimum(self.next_Q1_with_actor, self.next_Q2_with_actor, 'Q_with_actor')

        self.init_target_op, self.update_target_op = self._target_net_ops()

    def _target_net_ops(self):
        with tf.name_scope('target_net_op'):
            target_main_var_pairs = list(zip(self.target_variables, self.main_variables))
            init_target_op = list(map(lambda v: tf.assign(v[0], v[1], name='init_target_op'), target_main_var_pairs))
            update_target_op = list(map(lambda v: tf.assign(v[0], self.polyak * v[0] + (1. - self.polyak) * v[1], name='update_target_op'), target_main_var_pairs))

        return init_target_op, update_target_op   


class Temperature(Module):
    def __init__(self,
                 name,
                 args,
                 graph,
                 state,
                 next_state,
                 action,
                 next_action,
                 scope_prefix='',
                 log_tensorboard=False,
                 log_params=False):
        # next_* are used when the state value function is omitted
        self.state = state
        self.next_state = next_state
        self.action = action
        self.next_action = next_action
        self.type = args['type']
        super().__init__(name, 
                         args, 
                         graph, 
                         scope_prefix=scope_prefix,
                         log_tensorboard=log_tensorboard,
                         log_params=log_params)

    """ Implementation """
    def _build_graph(self):
        def simple_alpha():
            with tf.variable_scope('net'):
                log_alpha = tf.get_variable('log_alpha', dtype=tf.float32, initializer=0.)
                alpha = tf.exp(log_alpha)

            return log_alpha, alpha

        def state_alpha(state, reuse=False):
            with tf.variable_scope('net', reuse=reuse):
                x = state
                x = self.dense(x, 1)

                log_alpha = x
                alpha = tf.exp(log_alpha)
            
            return log_alpha, alpha

        def state_action_alpha(state, action, reuse=False):
            with tf.variable_scope('net', reuse=reuse):
                x = tf.concat([state, action], axis=1)
                x = self.dense(x, 1)

                log_alpha = x
                alpha = tf.exp(log_alpha)
            
            return log_alpha, alpha

        """ Function Body """
        if self.type == 'simple':
            self.log_alpha, self.alpha = simple_alpha()
            self.next_alpha = self.alpha
        elif self.type == 'state':
            self.log_alpha, self.alpha = state_alpha(self.state)
            _, self.next_alpha = state_alpha(self.next_state, reuse=True)
        elif self.type == 'state_action':
            self.log_alpha, self.alpha = state_action_alpha(self.state, self.action)
            _, self.next_alpha = state_action_alpha(self.next_state, self.next_action, reuse=True)
        else:
            raise NotImplementedError(f'Invalid type: {self.type}')
