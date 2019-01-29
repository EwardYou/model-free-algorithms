import numpy as np
import tensorflow as tf
import ray

from utils.losses import huber_loss
from basic_model.model import Model
from actor_critic import Actor, Critic
from gym_env.env import GymEnvironment

class Agent(Model):
    def __init__(self,
                 name,
                 args,
                 env_args,
                 sess_config=None,
                 reuse=None,
                 save=True,
                 log_tensorboard=False,
                 log_params=False):
        # hyperparameters
        self._gamma = args['gamma']
        self._advantage_discount = self._gamma * args['lambda']
        self._batch_size = args['batch_size']
        self._n_updates_per_iteration = args['n_updates_per_iteration']

        # environment info
        self.env = GymEnvironment(env_args['name'])
        self._max_path_length = (env_args['max_episode_steps'] if 'max_episode_steps' in env_args 
                                 else self.env.max_episode_steps)

        super().__init__(name, args, sess_config=sess_config,
                         reuse=reuse, save=True, 
                         log_tensorboard=log_tensorboard,
                         log_params=log_params)

        with self._graph.as_default():
            self.variables = ray.experimental.TensorFlowVariables(self.loss, self.sess)

    """ Implementation """
    def _build_graph(self, **kwargs):
        self.env_phs = self._setup_env_placeholders(self.env.observation_dim, self.env.action_dim)

        self.actor = Actor('actor', self._args['actor'], self._graph,
                        self.env_phs['observation'], self.env_phs['advantage'], 
                        self.env, self.name, reuse=self._reuse)
        self.critic = Critic('critic', self._args['critic'], self._graph,
                            self.env_phs['observation'], self.env_phs['return'],
                            self.name, self._reuse)

        self.action = self.actor.action

        self.loss = tf.add(self.actor.loss, self.critic.loss, name='total_loss')

        self.optimizer, self.global_step = self._adam_optimizer()

        self.grads_and_vars = self._compute_gradients(self.loss, self.optimizer)

        self.opt_op = self._apply_gradients(self.optimizer, self.grads_and_vars, self.global_step)

        print(self._args['model_name'], 'has been successfully constructed!')

    def _setup_env_placeholders(self, observation_dim, action_dim):
        env_phs = {}

        with tf.name_scope('placeholder'):
            env_phs['observation'] = tf.placeholder(tf.float32, shape=[None, observation_dim], name='observation')
            env_phs['return'] = tf.placeholder(tf.float32, shape=[None], name='return')
            env_phs['advantage'] = tf.placeholder(tf.float32, shape=[None], name='advantage')
        
        return env_phs

        