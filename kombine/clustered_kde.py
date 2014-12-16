import numpy as np
from scipy.misc import logsumexp
from scipy import linalg as la
from scipy.cluster.vq import kmeans, vq

import multiprocessing as mp


class OptimizedKDE(object):
    """
    Iteratively run a k-means clustering algorithm, estimating the distibution
    of each identified cluster with an independent kernel density estimate.
    Starting with k = 1, the distribution is estimated and the Bayes
    Information criterion (BIC) is calculated.  k is increased until the BIC
    stops increasing.  All methods of the ClusteredKDE class are then
    inherited.

    :param data:
        An N x ndim array, containing N samples from the target distribution.

    """
    def __init__(self, data, pool=None):
        best_bic = -np.inf
        best_kde = None

        k = 1
        while True:
            kde = ClusteredKDE(data, k, pool)
            bic = kde.bic()

            if (bic > best_bic):
                best_kde = kde
                best_bic = bic
            else:
                break
            k += 1

        self.kde = best_kde

    def __getattr__(self, attr):
        return getattr(self.kde, attr)

    def logpdf(self, X):
        return self.kde(X)

    __call__ = logpdf


class ClusteredKDE(object):
    """
    Run a k-means clustering algorithm, estimating the distibution of each
    identified cluster with an independent kernel density estimate.  The full
    distibution is then estimated by combining the individual KDE's, weighted
    by the fraction of samples assigned to each cluster.

    :param data:
        An N x ndim array, containing N samples from the target distribution.

    :param k:
        The number of clusters to use in the k-means clustering.

    """
    def __init__(self, data, k=1, pool=None):
        N, dim = data.shape
        self._N = N
        self._dim = dim
        self._k = k

        self._mean = np.mean(data, axis=0)
        self._std = np.std(data, axis=0)
        self._data = self._whiten(data)

        self._centroids, _ = kmeans(self._data, k)
        self._assignments, _ = vq(self._data, self._centroids)

        self._kdes = [KDE(self._data[self._assignments == c], pool=pool)
                      for c in range(k)]
        self._logweights = np.log(
            [np.sum(self._assignments == c)/float(self._N) for c in range(k)])

    def draw(self, N=1):
        # Draws clusters randomly with the assigned weights
        cumulative_weights = np.cumsum(np.exp(self._logweights))
        clusters = np.searchsorted(cumulative_weights, np.random.rand(N))

        draws = np.empty((N, self._dim))
        for c in xrange(self._k):
            sel = clusters == c
            draws[sel] = self._kdes[c].draw(np.sum(sel))

        return self._color(draws)

    def _whitened_logpdf(self, X):
        logpdfs = [logweight + kde(X)
                   for logweight, kde in zip(self._logweights, self._kdes)]
        return logsumexp(logpdfs, axis=0)

    def logpdf(self, X):
        return self._whitened_logpdf(self._whiten(X))

    def _whiten(self, data):
        return (data - self._mean)/self._std

    def _color(self, data):
        return data * self._std + self._mean

    def bic(self):
        log_l = np.sum(self._whitened_logpdf(self._data))

        # Determine the total number of parameters in clustered-KDE
        # Account for centroid locations
        nparams = self._k * self._dim

        # One for each cluster, minus one for constraint that all sum to unity
        nparams += self._k - 1

        # Separate kernel covariances for each cluster
        nparams += self._k * (self._dim + 1) * self._dim/2.0

        return log_l - nparams/2.0 * np.log(self._N)

    def size(self):
        return self._N

    __call__ = logpdf

    __len__ = size


class KDE(object):
    """
    A Gaussian kernel density estimator that provides means for evaluating
    the estimated probability density function, and drawing additional samples
    from the estimated distribution.  Cholesky decomposition of the covariance
    makes this class a bit more stable than the scipy KDE.

    :param data:
        An N x ndim array, containing N samples from the target distribution.

    """
    def __init__(self, data, pool=None):
        N, dim = data.shape
        self._N = N
        self._dim = dim
        self._data = data
        self._pool = pool

        self._mean = np.mean(data, axis=0)
        self._cov = np.cov(data, rowvar=0)

        self._set_bandwidth()

    def __enter__(self):
        return self

    def _set_bandwidth(self):
        """
        Use Scott's rule to set the kernel bandwidth.  Also store Cholesky
        decomposition for later.
        """
        kernel_cov = self._cov * self._N ** (-2./(self._dim + 4))

        # Used to evaluate PDF with cho_solve()
        self._cho_factor = la.cho_factor(kernel_cov)

        # Used for drawing samples
        self._decomposed_kernel_cov = la.cholesky(kernel_cov)

        # Make sure the estimated PDF integrates to 1.0
        self._lognorm = np.log(
            self._N * np.sqrt((2*np.pi ** self._dim) * la.det(kernel_cov)))

    def draw(self, N=1):
        """
        Draw samples from the estimated distribution.
        """
        # Draw from a unit Gaussian
        X = np.random.normal(size=(N, self._dim))

        # Pick N random kernels
        kernels = np.random.randint(0, self._N, N)

        # Recolor unit draws with the selected kernels
        draws = np.dot(self._decomposed_kernel_cov, X.T).T + \
            self._data[kernels]

        return draws

    def logpdf(self, X):
        N, dim = X.shape
        assert dim == self._dim

        # Apply across the pool if it exists
        if self._pool:
            M = self._pool.map
        else:
            M = map

        args = [(x, self._data, self._cho_factor) for x in X]
        results = M(_evaluate_point_logpdf, args)

        # Normalize and return
        return np.array(results) - self._lognorm

    __call__ = logpdf


def _evaluate_point_logpdf(args):
    """
    Evaluate the Gaussian KDE at a given point ``x''.  This lives
    outside the KDE method to allow for parallelization using
    ``multipocessing``. Since the ``map`` function only allows single-argument
    functions, the following arguments to be packed into a single tuple.

    :param x:
    The point to evaluate the KDE at.

    :param data:
    The N x dim array of data used to construct the KDE.

    :param cho_factor:
    A Cholesky decomposition of the kernel covariance matrix.

    """
    x, data, cho_factor = args

    # Use Cholesky decomposition to avoid direct inversion of covariance matrix
    diff = data - x
    tdiff = la.cho_solve(cho_factor, diff.T, check_finite=False).T
    diff *= tdiff

    # Work in the log to avoid large numbers
    return logsumexp(-np.sum(diff, axis=1)/2.0)