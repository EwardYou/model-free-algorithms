import numpy as np
import ray

from utils.np_math import norm
from agent import Agent

@ray.remote
class Worker(Agent):
    """ Interface """
    def __init__(self,
                 name,
                 args,
                 env_args,
                 sess_config=None,
                 reuse=False,
                 save=True):
        super().__init__(name, args, env_args, sess_config=sess_config, 
                         reuse=reuse, save=save)

        self._init_data()

    def compute_gradients(self, weights):
        assert (isinstance(self.obs, np.ndarray)
                and isinstance(self.actions, np.ndarray)
                and isinstance(self.returns, np.ndarray)
                and isinstance(self.advantages, np.ndarray) 
                and isinstance(self.old_neglogpi, np.ndarray))

        self._set_weights(weights)

        indices = np.random.choice(len(self.obs), self._batch_size)
        sample_obs = self.obs[indices]
        sample_actions = self.actions[indices]
        sample_returns = self.returns[indices]
        sample_advantages = self.advantages[indices]
        sample_old_neglogpi = self.old_neglogpi[indices]

        grads = self.sess.run(
            [grad_and_var[0] for grad_and_var in self.grads_and_vars],
            feed_dict={
                self.env_phs['observation']: sample_obs,
                self.actor.action: sample_actions,
                self.env_phs['return']: sample_returns,
                self.env_phs['advantage']: sample_advantages,
                self.actor.old_neglogpi_ph: sample_old_neglogpi
            })
        
        return grads

    def sample_trajectories(self, weights):
        # helper functions
        def sample_data(env, max_n_samples, max_path_length):
            obs, actions, values, rewards, old_neglogpi, nonterminals = [], [], [], [], [], []

            n_episodes = 0
            while len(obs) < max_n_samples:
                ob = env.reset()

                for _ in range(max_path_length):
                    obs.append(ob)
                    action, value, neglogpi = self.step(ob)
                    ob, reward, done, _ = env.step(action)
                    
                    actions.append(action)
                    values.append(value)
                    old_neglogpi.append(neglogpi)
                    rewards.append(reward)
                    nonterminals.append(1 - done)

                    if done:
                        break
                n_episodes += 1
                nonterminals[-1] = 0
            # add one more ad hoc state value so that we can take values[1:] as next state values
            if done:
                ob = self.env.reset()
            _, value, _ = self.step(ob)
            values.append(value)

            avg_score = np.sum(rewards) / n_episodes

            return avg_score, (np.asarray(obs, dtype=np.float32),
                                np.reshape(actions, [len(obs), -1]),
                                np.asarray(old_neglogpi, dtype=np.float32),
                                np.asarray(rewards, dtype=np.float32),
                                np.asarray(values, dtype=np.float32),
                                np.asarray(nonterminals, dtype=np.uint8))
        
        def compute_returns_advantages(rewards, values, nonterminals, gamma):
            if self._args['option']['advantage_type'] == 'norm':
                returns = rewards
                next_return = 0
                for i in reversed(range(len(rewards))):
                    returns[i] = rewards[i] + nonterminals[i] * gamma * next_return
                    next_return = returns[i]

                # normalize returns and advantages
                values = norm(values[:-1], np.mean(returns), np.std(returns))
                advantages = norm(returns - values)
                returns = norm(returns)
            elif self._args['option']['advantage_type'] == 'gae':
                deltas = rewards + nonterminals * self._gamma * values[1:] - values[:-1]
                advantages = deltas
                for i in reversed(range(len(rewards) - 1)):
                    advantages[i] += nonterminals[i] * self._advantage_discount * advantages[i+1]
                returns = advantages + values[:-1]

            # return norm(returns), norm(advantages)
            return returns, advantages

        # function content
        self._set_weights(weights)
        self._init_data()

        avg_score, data = sample_data(self.env, 
                                    self._n_updates_per_iteration * self._batch_size,
                                    self._max_path_length)
        self.obs, self.actions, self.old_neglogpi, rewards, values, nonterminals = data

        self.returns, self.advantages = compute_returns_advantages(rewards, values, nonterminals, self._gamma)

        return avg_score

    def step(self, observation):
        observation = np.reshape(observation, (-1, self.env.observation_dim))
        action, value, neglogpi = self.sess.run([self.action, self.critic.V, self.actor.neglogpi], 
                                            feed_dict={self.env_phs['observation']: observation})
        return np.squeeze(action), value, neglogpi

    """ Implementation """
    def _set_weights(self, weights):
        self.variables.set_flat(weights)
        
    def _init_data(self):
        self.obs, self.actions, self.returns, self.advantages, self.old_neglogpi = [], [], [], [], []