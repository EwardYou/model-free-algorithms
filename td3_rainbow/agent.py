import numpy as np
import tensorflow as tf
import tensorflow.contrib as tc

from utility import tf_utils
from basic_model.model import Model
from gym_env.env import GymEnvironment
from actor_critic import Actor, Critic, DoubleCritic
from utility.debug_tools import timeit
from utility.losses import huber_loss


class Agent(Model):
    """ Interface """
    def __init__(self, name, 
                 args, 
                 env_args, 
                 buffer, 
                 sess_config=None, 
                 reuse=None, 
                 log_tensorboard=True, 
                 save=True, 
                 log_params=False, 
                 log_score=True, 
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

        # environment info
        self.env = GymEnvironment(env_args['name'])
        self._max_path_length = (env_args['max_episode_steps'] if 'max_episode_steps' in env_args 
                                 else self.env.max_episode_steps)
        self._state_dim = self.env.state_dim
        self._action_dim = self.env.action_dim
        
        # replay buffer
        self.buffer = buffer

        super().__init__(name, args, sess_config=sess_config, reuse=reuse,
                         log_tensorboard=log_tensorboard, save=save, 
                         log_params=log_params, log_score=log_score, device=device)

        self._initialize_target_net()

    @property
    def main_variables(self):
        return self.actor.trainable_variables + self.critic.trainable_variables

    @property
    def _target_variables(self):
        return self._target_actor.trainable_variables + self._target_critic.trainable_variables

    def act(self, state):
        state = state.reshape((-1, self._state_dim))
        action = self.sess.run(self.actor.action, feed_dict={self.actor.state: state})

        return np.squeeze(action)

    def learn(self, state, action, reward, next_state, done):
        self.buffer.add(state, action, reward, next_state, done)

        if self.trainable and self.buffer.good_to_learn:
            self._learn()
    
    """ Implementation """
    def _build_graph(self, **kwargs):
        self.data = self._setup_env_placeholders()
        
        self.actor, self.critic, self._target_actor, self._target_critic = self._create_main_target_actor_critic()

        self.priorities, self.actor_loss, self.critic_loss = self._loss()
    
        self.opt_op = self._optimize_op(self.actor_loss, self.critic_loss)

        # target net operations
        self.init_target_op, self.update_target_op = self._target_net_ops()

        self._log_loss()

    def _setup_env_placeholders(self):
        data = {}
        with tf.name_scope('placeholders'):
            data['state'] = tf.placeholder(tf.float32, shape=(None, self._state_dim), name='state')
            data['action'] = tf.placeholder(tf.float32, shape=(None, self._action_dim), name='action')
            data['next_state'] = tf.placeholder(tf.float32, shape=(None, self._state_dim), name='next_state')
            data['rewards'] = tf.placeholder(tf.float32, shape=(None, self.n_steps, 1), name='rewards')
            data['done'] = tf.placeholder(tf.float32, shape=(None, 1), name='done')
            data['steps'] = tf.placeholder(tf.float32, shape=(None, 1), name='steps')
            if self._buffer_type != 'uniform':
                data['IS_ratio'] = tf.placeholder(tf.float32, shape=(self.batch_size), name='importance_sampling_ratio')
        
        return data

    def _create_main_target_actor_critic(self):
        # main actor-critic
        actor, critic = self._create_actor_critic(is_target=False, double_Q=self._double_Q)
        # target actor-critic
        target_actor, target_critic = self._create_actor_critic(is_target=True, double_Q=self._double_Q)

        return actor, critic, target_actor, target_critic
        
    def _create_actor_critic(self, is_target=False, double_Q=False):
        log_tensorboard = False if is_target else self._log_tensorboard
        log_params = False if is_target else self._log_params

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
                          scope_prefix=scope_prefix, 
                          log_tensorboard=log_tensorboard, 
                          log_params=log_params)

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
                                 scope_prefix=scope_prefix, 
                                 log_tensorboard=log_tensorboard,
                                 log_params=log_params)
        
        return actor, critic

    def _loss(self):
        with tf.name_scope('loss'):
            with tf.name_scope('actor_loss'):
                Q_with_actor = self.critic.Q1_with_actor if self._double_Q else self.critic.Q_with_actor
                actor_loss = tf.negative(tf.reduce_mean(Q_with_actor), name='actor_loss')

            with tf.name_scope('critic_loss'):
                critic_loss_func = self._double_critic_loss if self._double_Q else self._plain_critic_loss
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

    def _n_step_target(self, nth_Q):
        rewards_sum = tf.reduce_sum(self.data['rewards'], axis=1)
        n_step_target = tf.add(rewards_sum, self.gamma**self.data['steps']
                                            * (1 - self.data['done'])
                                            * nth_Q, name='target_Q')

        return tf.stop_gradient(n_step_target)

    def _optimize_op(self, actor_loss, critic_loss):
        with tf.variable_scope('learn_steps', reuse=self._reuse):
            self.learn_steps = tf.get_variable('learn_steps', shape=[], 
                                               initializer=tf.constant_initializer(), trainable=False)
            step_op = tf.assign(self.learn_steps, self.learn_steps + 1, name='update_learn_steps')

        with tf.variable_scope('optimizer', reuse=self._reuse):
            actor_opt_op, _ = self.actor._optimization_op(actor_loss)
            critic_opt_op, _ = self.critic._optimization_op(critic_loss)

            with tf.control_dependencies([step_op]):
                opt_op = tf.group(actor_opt_op, critic_opt_op)

        return opt_op

    def _target_net_ops(self):
        with tf.name_scope('target_net_op'):
            target_main_var_pairs = list(zip(self._target_variables, self.main_variables))
            init_target_op = list(map(lambda v: tf.assign(v[0], v[1], name='init_target_op'), target_main_var_pairs))
            update_target_op = list(map(lambda v: tf.assign(v[0], self.tau * v[1] + (1. - self.tau) * v[0], name='update_target_op'), target_main_var_pairs))

        return init_target_op, update_target_op

    def _learn(self):
        def env_feed_dict():
            if self._buffer_type == 'uniform':
                feed_dict = {}
            else:
                feed_dict = {self.data['IS_ratio']: IS_ratios}

            feed_dict.update({
                self.data['state']: states,
                self.data['action']: actions,
                self.data['rewards']: rewards,
                self.data['next_state']: next_states,
                self.data['done']: dones,
                self.data['steps']: steps
            })

            return feed_dict

        IS_ratios, saved_exp_ids, (states, actions, rewards, next_states, dones, steps) = self.buffer.sample()

        feed_dict = env_feed_dict()

        # update the main networks
        if self._log_tensorboard:
            priorities, learn_steps, _, summary = self.sess.run([self.priorities, self.learn_steps, self.opt_op, self.graph_summary], feed_dict=feed_dict)
            if learn_steps % 100 == 0:
                self.writer.add_summary(summary, learn_steps)
                self.save()
        else:
            priorities, _ = self.sess.run([self.priorities, self.opt_op], feed_dict=feed_dict)

        if self._buffer_type != 'uniform':
            self.buffer.update_priorities(priorities, saved_exp_ids)

        # update the target networks
        self.sess.run(self.update_target_op)

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
                tf.summary.scalar('max_Q_with_actor', tf.reduce_max(self.critic.Q_with_actor))
                tf.summary.scalar('min_Q_with_actor', tf.reduce_min(self.critic.Q_with_actor))
                tf.summary.scalar('Q_with_actor_', tf.reduce_mean(self.critic.Q_with_actor))
            