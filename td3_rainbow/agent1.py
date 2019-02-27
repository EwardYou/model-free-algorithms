import numpy as np
import tensorflow as tf
import tensorflow.contrib as tc

from utility import tf_utils
from basic_model.model import Model
from actor_critic import Actor, Critic, DoubleCritic
from utility.debug_tools import timeit
from utility.losses import huber_loss
from gym_env.env import GymEnvironment


class Agent(Model):
    """ Interface """
    def __init__(self, 
                 name, 
                 args, 
                 env_args,
                 buffer,
                 sess_config=None, 
                 reuse=None, 
                 save=True, 
                 log_tensorboard=False, 
                 log_params=False,
                 log_score=False,
                 device=None):
        # hyperparameters
        self.gamma = args['gamma'] if 'gamma' in args else .99 
        self.tau = args['tau'] if 'tau' in args else 1e-3
        self.batch_size = args['batch_size'] if 'batch_size' in args else 128

        # options for DDPG improvements
        options = args['options']
        self._buffer_type = options['buffer_type']
        self._double_Q = options['double_Q']
        self.n_steps = options['n_steps']

        self._critic_loss_type = args['critic']['loss_type']
        self._critic_update_times = args['critic']['update_times']

        # environment info
        self.env = GymEnvironment(env_args['name'])
        self._max_path_length = (env_args['max_episode_steps'] if 'max_episode_steps' in env_args 
                                 else self.env.max_episode_steps)
        self._state_dim = self.env.state_dim
        self._action_dim = self.env.action_dim

        self.buffer = buffer

        super().__init__(name, args, sess_config=sess_config, 
                         reuse=reuse, save=save, 
                         log_tensorboard=log_tensorboard, 
                         log_params=log_params,
                         log_score=log_score,
                         device=device)

        self._initialize_target_net()

        # with self._graph.as_default():
        #     self.variables = ray.experimental.TensorFlowVariables(self.loss, self.sess)

    @property
    def main_variables(self):
        return self.actor.trainable_variables + self.critic.trainable_variables

    @property
    def _target_variables(self):
        return self._target_actor.trainable_variables + self._target_critic.trainable_variables

    def act(self, state):
        state = state.reshape((-1, self.env.state_dim))
        action = self.sess.run(self.actor.action, feed_dict={self.actor.state: state})

        return np.squeeze(action)

    def learn(self, state, action, reward, next_state, done):
        self.buffer.add(state, action, reward, next_state, done)

        if self.trainable and self.buffer.good_to_learn:
            self._learn()

    """ Implementation """
    def _build_graph(self, **kwargs):
        with tf.device('/cpu: 2'):
            self.data = self._prepare_data(self.buffer)

        self.actor, self.critic, self._target_actor, self._target_critic = self._create_main_target_actor_critic(self._double_Q)

        self.priorities, self.actor_loss, self.critic_loss = self._loss(self.critic, self._double_Q)

        self.actor_opt_op, self.global_step = self.actor._optimization_op(self.actor_loss, global_step=True)
        self.critic_opt_op, _ = self.critic._optimization_op(self.critic_loss)
        
        # target net operations
        self.init_target_op, self.update_target_op = self._target_net_ops()

        self._log_loss()

    def _create_main_target_actor_critic(self, double_Q):
        # main actor-critic
        actor, critic = self._create_actor_critic(is_target=False, double_Q=double_Q)
        # target actor-critic
        target_actor, target_critic = self._create_actor_critic(is_target=True, double_Q=double_Q)

        return actor, critic, target_actor, target_critic
        
    def _create_actor_critic(self, is_target, double_Q):
        scope_name = 'target' if is_target else 'main'
        state = self.data['next_state'] if is_target else self.data['state']
        scope_prefix = self.name + '/' + scope_name
        with tf.variable_scope(scope_name, reuse=self._reuse):
            actor = Actor('actor', 
                          self._args['actor'], 
                          self._graph, 
                          state, 
                          self._action_dim,
                          reuse=self._reuse,
                          is_target=is_target,
                          scope_prefix=scope_prefix)

            critic_type = (DoubleCritic if double_Q else Critic)
            critic = critic_type('critic', 
                                self._args['critic'], 
                                self._graph,
                                state, 
                                self.data['action'], 
                                actor.action,
                                self._action_dim,
                                reuse=self._reuse,
                                is_target=is_target, 
                                scope_prefix=scope_prefix)

        return actor, critic

    def _prepare_data(self, buffer):
        with tf.name_scope('data'):
            sample_types = (tf.float32, tf.int32, (tf.float32, tf.float32, tf.float32, tf.float32, tf.float32, tf.float32))
            sample_shapes =((self.batch_size), (self.batch_size), (
                (None, self._state_dim),
                (None, self._action_dim),
                (None, self.n_steps, 1),
                (None, self._state_dim),
                (None, 1),
                (None, 1)
            ))
            ds = tf.data.Dataset.from_generator(buffer, sample_types, sample_shapes)
            ds = ds.prefetch(1)
            iterator = ds.make_one_shot_iterator()
            samples = iterator.get_next(name='samples')
        
        # prepare data
        IS_ratio, saved_exp_ids, (state, action, reward, next_state, done, steps) = samples

        data = {}
        data['IS_ratio'] = IS_ratio
        data['saved_exp_ids'] = saved_exp_ids
        data['state'] = state
        data['action'] = action
        data['reward'] = reward
        data['next_state'] = next_state
        data['done'] = done
        data['steps'] = steps

        return data

    """ Losses """
    def _loss(self, critic, double_Q):
        with tf.name_scope('loss'):
            with tf.name_scope('actor_loss'):
                Q_with_actor = critic.Q1_with_actor if double_Q else critic.Q_with_actor
                actor_loss = tf.negative(tf.reduce_mean(Q_with_actor), name='actor_loss')

            with tf.name_scope('critic_loss'):
                critic_loss_func = self._double_critic_loss if double_Q else self._plain_critic_loss
                priorities, critic_loss = critic_loss_func()

        return priorities, actor_loss, critic_loss

    def _double_critic_loss(self):
        target_Q = self._n_step_target(self._target_critic.Q_with_actor)
        
        TD_error1 = tf.abs(target_Q - self.critic.Q1, name='TD_error1')
        TD_error2 = tf.abs(target_Q - self.critic.Q2, name='TD_error2')
        priorities = tf.divide(TD_error1 + TD_error2, 2., name='priorities')

        loss_func = huber_loss if self._critic_loss_type == 'huber' else tf.square
        TD_squared = loss_func(TD_error1) + loss_func(TD_error2)
        
        critic_loss = self._average_critic_loss(TD_squared)

        return priorities, critic_loss
        
    def _plain_critic_loss(self):
        target_Q = self._n_step_target(self._target_critic.Q_with_actor)
        
        TD_error = tf.abs(target_Q - self.critic.Q, name='TD_error')
        priorities = tf.identity(TD_error, name='priorities')

        loss_func = huber_loss if self._critic_loss_type == 'huber' else tf.square
        TD_squared = loss_func(TD_error)

        critic_loss = self._average_critic_loss(TD_squared)
        
        return priorities, critic_loss

    def _average_critic_loss(self, loss):
        weighted_loss = loss if self._buffer_type == 'uniform' else self.data['IS_ratio'] * loss

        critic_loss = tf.reduce_mean(weighted_loss, name='critic_loss')

        return critic_loss

    def _n_step_target(self, n_step_value):
        rewards_sum = tf.reduce_sum(self.data['reward'], axis=1)
        n_step_gamma = self.gamma**self.data['steps']
        n_step_target = tf.add(rewards_sum, n_step_gamma
                                            * (1. - self.data['done'])
                                            * n_step_value, name='target_Q')
        
        return tf.stop_gradient(n_step_target)

    def _target_net_ops(self):
        with tf.name_scope('target_net_op'):
            target_main_var_pairs = list(zip(self._target_variables, self.main_variables))
            init_target_op = list(map(lambda v: tf.assign(v[0], v[1], name='init_target_op'), target_main_var_pairs))
            update_target_op = list(map(lambda v: tf.assign(v[0], self.tau * v[1] + (1. - self.tau) * v[0], name='update_target_op'), target_main_var_pairs))

        return init_target_op, update_target_op

    def _initialize_target_net(self):
        self.sess.run(self.init_target_op)

    def _log_loss(self):
        if self._log_tensorboard:
            if self._buffer_type != 'uniform':
                with tf.name_scope('priority'):
                    tf.summary.histogram('priorities_', self.priorities)
                    tf.summary.scalar('priority_', tf.reduce_mean(self.priorities))

                with tf.name_scope('IS_ratio'):
                    tf.summary.histogram('IS_ratios_', self.data['IS_ratio'])
                    tf.summary.scalar('IS_ratio_', tf.reduce_mean(self.data['IS_ratio']))

            with tf.variable_scope('loss', reuse=self._reuse):
                tf.summary.scalar('actor_loss_', self.actor_loss)
                tf.summary.scalar('critic_loss_', self.critic_loss)
            
            with tf.name_scope('Q'):
                tf.summary.scalar('max_Q_with_actor', tf.reduce_max(self.critic.Q1_with_actor))
                tf.summary.scalar('min_Q_with_actor', tf.reduce_min(self.critic.Q1_with_actor))
                tf.summary.scalar('Q_with_actor_', tf.reduce_mean(self.critic.Q1_with_actor))

    def _learn(self):
        # update critic a few times first
        for _ in range(self._critic_update_times-1):
            priorities, saved_exp_ids, _ = self.sess.run([self.priorities, 
                                                          self.data['saved_exp_ids'], 
                                                          self.critic_opt_op])
            if self._buffer_type != 'uniform':
                self.buffer.update_priorities(priorities)

        # update the main networks
        if self._log_tensorboard:
            priorities, saved_exp_ids, learn_steps, _, _, summary = self.sess.run(
                [self.priorities, 
                self.data['saved_exp_ids'],
                self.global_step, 
                self.critic_opt_op, 
                self.actor_opt_op, 
                self.graph_summary]
            )
            if learn_steps % 100 == 0:
                self.writer.add_summary(summary, learn_steps)
                self.save()
        else:
            priorities, saved_exp_ids, _, _ = self.sess.run(
                [self.priorities, 
                self.data['saved_exp_ids'], 
                self.critic_opt_op, 
                self.actor_opt_op]
            ) 
                
        if self._buffer_type != 'uniform':
            self.buffer.update_priorities(priorities, saved_exp_ids)

        # update the target networks
        self.sess.run(self.update_target_op)
