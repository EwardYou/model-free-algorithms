import numpy as np
import tensorflow as tf

EPSILON = 1e-8


def tf_scope(func):
    def name_scope(*args):
        with tf.name_scope(func.__name__):
            return func(*args)
    return name_scope

class Distribution():
    @tf_scope
    def logp(self, x):
        return -self._neglogp(x)

    @tf_scope
    def neglogp(self, x):
        return self._neglogp(x)

    @tf_scope
    def sample(self, *args, **kwargs):
        return self._sample(*args, **kwargs)
        
    @tf_scope
    def entropy(self):
        return self._entropy()

    @tf_scope
    def kl(self, other):
        assert isinstance(other, type(self))
        return self._kl(other)

    def _neglogp(self, x):
        raise NotImplementedError

    def _sample(self):
        raise NotImplementedError

    def _entropy(self):
        raise NotImplementedError

    def _kl(self, other):
        raise NotImplementedError


class Categorical(Distribution):
    def __init__(self, logits):
        self.logits = logits
        self.tau = tf.Variable(0, name='softmax_tau')

    def _neglogp(self, x):
        if len(x.shape.as_list()) == len(self.logits.shape.as_list()) and x.shape.as_list()[-1] != 1:
            return tf.nn.softmax_cross_entropy_with_logits_v2(labels=tf.stop_gradient(x), logits=self.logits)[..., None]
        else:
            x = tf.squeeze(x)
            return tf.nn.sparse_softmax_cross_entropy_with_logits(labels=x, logits=self.logits)[..., None]

    def _sample(self, reparameterize=False, hard=True, epsilon=1e-20):
        """
         A differentiable sampling method for categorical distribution
         reference paper: Categorical Reparameterization with Gumbel-Softmax
         and code: https://github.com/ericjang/gumbel-softmax/blob/master/Categorical%20VAE.ipynb
        """
        if reparameterize:
            # sample Gumbel(0, 1)
            U = tf.random_uniform(tf.shape(self.logits), minval=0, maxval=1)
            g = -tf.log(-tf.log(U+epsilon)+epsilon)
            # Draw a sample from the Gumbel-Softmax distribution
            y = tf.nn.softmax((self.logits + g) / self.tau)
            # draw one-hot encoded sample from the softmax
            if hard:
                y_hard = tf.cast(tf.equal(y, tf.reduce_max(y, 1, keepdims=True)), y.dtype)
                y = tf.stop_gradient(y_hard - y) + y
        else:
            y = tf.random.categorical(self.logits, 1, dtype=tf.int32)

        return y

    def _entropy(self):
        probs = self._compute_probs()
        entropy = tf.reduce_sum(-probs * tf.log(probs), axis=-1)

        return entropy

    def _kl(self, other):
        probs = self._compute_probs()
        other_probs = other._compute_probs()
        kl = tf.reduce_sum(probs * (tf.log(probs) - tf.log(other_probs)), axis=-1)

        return kl

    def _compute_probs(self):
        logits = self.logits - tf.reduce_max(self.logits, axis=-1, keepdims=True)
        exp_logits = tf.exp(logits)
        sum_exp_logits = tf.reduce_sum(exp_logits, axis=-1, keepdims=True)
        probs = exp_logits / sum_exp_logits

        return probs

class DiagGaussian(Distribution):
    def __init__(self, params):
        self.mean, self.logstd = params
        self.std = tf.exp(self.logstd)

    def _neglogp(self, x):
        return .5 * tf.reduce_sum(np.log(2. * np.pi)
                                  + 2 * self.logstd
                                  + ((x - self.mean) / (self.std + EPSILON))**2, 
                                  axis=-1, keepdims=True)

    def _sample(self, reparameterize=True):
        # TODO: implement sampling without reparameterization
        return self.mean + self.std * tf.random.normal(tf.shape(self.mean))

    def _entropy(self):
        return tf.reduce_sum(.5 * np.log(2. * np.pi) + self.logstd + .5, axis=-1)

    def _kl(self, other):
        return tf.reduce_sum(other.logstd - self.logstd - .5
                             + .5 * (self.std**2 + (self.mean - other.mean)**2) / (other.std + EPSILON)**2, axis=-1)

def compute_sample_mean_variance(samples, name='sample_mean_var'):
    """ Compute mean and covariance matrix from samples """
    sample_size = samples.shape.as_list()[0]
    with tf.name_scope(name):
        samples = tf.reshape(samples, [sample_size, -1])
        mean = tf.reduce_mean(samples, axis=0)
        samples_shifted = samples - mean
        # Following https://en.wikipedia.org/wiki/Estimation_of_covariance_matrices
        covariance = 1 / (sample_size - 1.) * tf.matmul(samples_shifted, samples_shifted, transpose_a=True)

        # Take into account case of zero covariance
        almost_zero_covariance = tf.fill(tf.shape(covariance), 1e-8)
        is_zero = tf.equal(tf.reduce_sum(tf.abs(covariance)), 0)
        covariance = tf.where(is_zero, almost_zero_covariance, covariance)

        return mean, covariance

def compute_kl_with_standard_gaussian(mean, covariance, name='kl_with_standard_gaussian'):
    """ Compute KL(N(mean, covariance) || N(0, I)) following 
    https://en.wikipedia.org/wiki/Multivariate_normal_distribution#Kullback%E2%80%93Leibler_divergence
    """
    vec_dim = mean.shape[-1]
    with tf.name_scope(name):
        trace = tf.trace(covariance)
        squared_term = tf.reduce_sum(tf.square(mean))
        logdet = tf.linalg.logdet(covariance)
        result = 0.5 * (trace + squared_term - vec_dim - logdet)

    return result
