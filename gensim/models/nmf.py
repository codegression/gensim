"""Online Non-Negative Matrix Factorization."""

import itertools

import logging
import numpy as np
import scipy.sparse
from scipy.stats import halfnorm

from gensim import interfaces
from gensim import matutils
from gensim import utils
from gensim.interfaces import TransformedCorpus
from gensim.models import basemodel
from gensim.models.nmf_pgd import solve_h, solve_r

logger = logging.getLogger(__name__)


class Nmf(interfaces.TransformationABC, basemodel.BaseTopicModel):
    """Online Non-Negative Matrix Factorization.
    """

    def __init__(
        self,
        corpus=None,
        num_topics=100,
        id2word=None,
        chunksize=2000,
        passes=1,
        lambda_=1.0,
        kappa=1.0,
        minimum_probability=0.01,
        use_r=False,
        store_r=False,
        w_max_iter=200,
        w_stop_condition=1e-4,
        h_r_max_iter=50,
        h_r_stop_condition=1e-3,
        eval_every=10,
        v_max=None,
        normalize=True,
        sparse_coef=3,
        random_state=None,
    ):
        """

        Parameters
        ----------
        corpus : iterable of list of (int, float), optional
            Training corpus. If not given, model is left untrained.
        num_topics : int
            Number of topics to extract.
        id2word: Dict[int, str], optional
            Mapping from token id to token. If not set words get replaced with word ids.
        chunksize: int, optional
            Number of documents to be used in each training chunk.
        passes: int, optioanl
            Number of full passes over the training corpus.
        lambda_ : float, optional
            Residuals regularizer coefficient. Increasing it helps prevent ovefitting. Has no effect if `use_r` is set
            to False.
        kappa : float, optional
            Optimizer step coefficient. Increaing it makes model train faster, but adds a risk that it won't converge.
        store_r : bool, optional
            Whether to save residuals during training.
        w_max_iter: int, optional
            Maximum number of iterations to train W matrix per each batch.
        w_stop_condition: float, optional
            If error difference gets less than that, training of matrix ``W`` stops for current batch.
        h_r_max_iter: int, optional
            Maximum number of iterations to train h and r matrices per each batch.
        h_r_stop_condition: float
            If error difference gets less than that, training of matrices ``h`` and ``r`` stops for current batch.
        eval_every: int, optional
            Number of batches after which model will be evaluated.
        v_max: int, optional
            Maximum number of word occurrences in the corpora. Inferred if not set. Rarely needs to be set explicitly.
        normalize: bool, optional
            Whether to normalize results. Offers "kind-of-probabilistic" result.
        sparse_coef: float, optional
            The more it is, the more sparse are matrices. Significantly increases performance.
        random_state: {np.random.RandomState, int}, optional
            Seed for random generator. Useful for reproducibility.
        """
        self._w_error = None
        self.n_features = None
        self.num_topics = num_topics
        self.id2word = id2word
        self.chunksize = chunksize
        self.passes = passes
        self._lambda_ = lambda_
        self._kappa = kappa
        self.minimum_probability = minimum_probability
        self.use_r = use_r
        self._w_max_iter = w_max_iter
        self._w_stop_condition = w_stop_condition
        self._h_r_max_iter = h_r_max_iter
        self._h_r_stop_condition = h_r_stop_condition
        self.v_max = v_max
        self.eval_every = eval_every
        self.normalize = normalize
        self.sparse_coef = sparse_coef
        self.random_state = utils.get_random_state(random_state)

        if self.id2word is None:
            self.id2word = utils.dict_from_corpus(corpus)

        self.A = None
        self.B = None

        self.w_std = None

        if store_r:
            self._R = []
        else:
            self._R = None

        if corpus is not None:
            self.update(corpus)

    def get_topics(self):
        """Get the term-topic matrix learned during inference.


        Returns
        -------
        numpy.ndarray
            The probability for each word in each topic, shape (`num_topics`, `vocabulary_size`).

        """
        if self.normalize:
            return self._W.T.toarray() / self._W.T.toarray().sum(axis=1).reshape(-1, 1)

        return self._W.T.toarray()

    def __getitem__(self, bow, eps=None):
        return self.get_document_topics(bow, eps)

    def show_topics(self, num_topics=10, num_words=10, log=False, formatted=True):
        """Get a representation for selected topics.

        Parameters
        ----------
        num_topics : int, optional
            Number of topics to be returned. Unlike LSA, there is no natural ordering between the topics in NMF.
            The returned topics subset of all topics is therefore arbitrary and may change between two NMF
            training runs.
        num_words : int, optional
            Number of words to be presented for each topic. These will be the most relevant words (assigned the highest
            probability for each topic).
        log : bool, optional
            Whether the output is also logged, besides being returned.
        formatted : bool, optional
            Whether the topic representations should be formatted as strings. If False, they are returned as
            2 tuples of (word, probability).

        Returns
        -------
        list of {str, tuple of (str, float)}
            a list of topics, each represented either as a string (when `formatted` == True) or word-probability
            pairs.

        """
        # TODO: maybe count sparsity in some other way
        sparsity = self._W.getnnz(axis=0)

        if num_topics < 0 or num_topics >= self.num_topics:
            num_topics = self.num_topics
            chosen_topics = range(num_topics)
        else:
            num_topics = min(num_topics, self.num_topics)

            sorted_topics = list(matutils.argsort(sparsity))
            chosen_topics = (
                sorted_topics[: num_topics // 2] + sorted_topics[-num_topics // 2 :]
            )

        shown = []

        topics = self.get_topics()

        # print(topics)

        for i in chosen_topics:
            topic = topics[i]
            # print(topic)
            # print(topic.shape)
            bestn = matutils.argsort(topic, num_words, reverse=True).ravel()
            # print(type(bestn))
            # print(bestn.shape)
            topic = [(self.id2word[id], topic[id]) for id in bestn]
            if formatted:
                topic = " + ".join(['%.3f*"%s"' % (v, k) for k, v in topic])

            shown.append((i, topic))
            if log:
                logger.info("topic #%i (%.3f): %s", i, sparsity[i], topic)

        return shown

    def show_topic(self, topicid, topn=10):
        """Get the representation for a single topic. Words here are the actual strings, in constrast to
        :meth:`~gensim.models.nmf.Nmf.get_topic_terms` that represents words by their vocabulary ID.

        Parameters
        ----------
        topicid : int
            The ID of the topic to be returned
        topn : int, optional
            Number of the most significant words that are associated with the topic.

        Returns
        -------
        list of (str, float)
            Word - probability pairs for the most relevant words generated by the topic.

        """
        return [
            (self.id2word[id], value)
            for id, value in self.get_topic_terms(topicid, topn)
        ]

    def get_topic_terms(self, topicid, topn=10):
        """Get the representation for a single topic. Words the integer IDs, in constrast to
        :meth:`~gensim.models.nmf.Nmf.show_topic` that represents words by the actual strings.

        Parameters
        ----------
        topicid : int
            The ID of the topic to be returned
        topn : int, optional
            Number of the most significant words that are associated with the topic.

        Returns
        -------
        list of (int, float)
            Word ID - probability pairs for the most relevant words generated by the topic.

        """
        topic = self.get_topics()[topicid]
        bestn = matutils.argsort(topic, topn, reverse=True)
        return [(idx, topic[idx]) for idx in bestn]

    def get_term_topics(self, word_id, minimum_probability=None):
        """Get the most relevant topics to the given word.

        Parameters
        ----------
        word_id : int
            The word for which the topic distribution will be computed.
        minimum_probability : float, optional
            Topics with an assigned probability below this threshold will be discarded.

        Returns
        -------
        list of (int, float)
            The relevant topics represented as pairs of their ID and their assigned probability, sorted
            by relevance to the given word.

        """
        if minimum_probability is None:
            minimum_probability = self.minimum_probability
        minimum_probability = max(minimum_probability, 1e-8)

        # if user enters word instead of id in vocab, change to get id
        if isinstance(word_id, str):
            word_id = self.id2word.doc2bow([word_id])[0][0]

        values = []

        for topic_id in range(0, self.num_topics):
            word_coef = self._W[word_id, topic_id]

            if word_coef >= minimum_probability:
                values.append((topic_id, word_coef))

        return values

    def get_document_topics(self, bow, minimum_probability=None):
        """Get the topic distribution for the given document.

        Parameters
        ----------
        bow : list of (int, float)
            The document in BOW format.
        minimum_probability : float
            Topics with an assigned probability lower than this threshold will be discarded.

        Returns
        -------
        list of (int, float)
            Topic distribution for the whole document. Each element in the list is a pair of a topic's id, and
            the probability that was assigned to it.

        """
        if minimum_probability is None:
            minimum_probability = self.minimum_probability
        minimum_probability = max(minimum_probability, 1e-8)

        # if the input vector is a corpus, return a transformed corpus
        is_corpus, corpus = utils.is_corpus(bow)

        if is_corpus:
            kwargs = dict(minimum_probability=minimum_probability)
            return self._apply(corpus, **kwargs)

        v = matutils.corpus2csc([bow], len(self.id2word)).tocsr()
        h, _ = self._solveproj(v, self._W, v_max=np.inf)

        if self.normalize:
            h.data /= h.sum()

        return [
            (idx, proba.toarray()[0, 0])
            for idx, proba in enumerate(h[:, 0])
            if not minimum_probability or proba.toarray()[0, 0] > minimum_probability
        ]

    def _setup(self, corpus):
        """Infer info from the first document and initialize matrices.

        Parameters
        ----------
        corpus : iterable of list(int, float)
            Training corpus.

        """
        self._h, self._r = None, None
        first_doc_it = itertools.tee(corpus, 1)
        first_doc = next(first_doc_it[0])
        first_doc = matutils.corpus2csc([first_doc], len(self.id2word))[:, 0]
        self.n_features = first_doc.shape[0]
        self.w_std = np.sqrt(first_doc.mean() / (self.n_features * self.num_topics))

        self._W = np.abs(
            self.w_std
            * halfnorm.rvs(
                size=(self.n_features, self.num_topics), random_state=self.random_state
            )
        )

        is_great_enough = self._W > self.w_std * self.sparse_coef

        self._W *= is_great_enough | ~is_great_enough.all(axis=0)

        self._W = scipy.sparse.csc_matrix(self._W)

        self.A = scipy.sparse.csr_matrix((self.num_topics, self.num_topics))
        self.B = scipy.sparse.csc_matrix((self.n_features, self.num_topics))

    def update(self, corpus, chunks_as_numpy=False):
        """Train the model with new documents.

        Parameters
        ----------
        corpus : iterable of list(int, float)
            Training corpus.
        chunks_as_numpy : bool, optional
            Whether each chunk passed to the inference step should be a numpy.ndarray or not. Numpy can in some settings
            turn the term IDs into floats, these will be converted back into integers in inference, which incurs a
            performance hit. For distributed computing it may be desirable to keep the chunks as `numpy.ndarray`.
        """

        if self.n_features is None:
            self._setup(corpus)

        chunk_idx = 1

        for _ in range(self.passes):
            for chunk in utils.grouper(
                corpus, self.chunksize, as_numpy=chunks_as_numpy
            ):
                self.random_state.shuffle(chunk)
                v = matutils.corpus2csc(chunk, len(self.id2word)).tocsr()
                self._h, self._r = self._solveproj(
                    v, self._W, r=self._r, h=self._h, v_max=self.v_max
                )
                h, r = self._h, self._r
                if self._R is not None:
                    self._R.append(r)

                self.A *= chunk_idx - 1
                self.A += h.dot(h.T)
                self.A /= chunk_idx

                self.B *= chunk_idx - 1
                self.B += (v - r).dot(h.T)
                self.B /= chunk_idx

                self._solve_w()

                if chunk_idx % self.eval_every == 0:
                    logger.info(
                        "Loss (no outliers): {}\tLoss (with outliers): {}".format(
                            scipy.sparse.linalg.norm(v - self._W.dot(h)),
                            scipy.sparse.linalg.norm(v - self._W.dot(h) - r),
                        )
                    )

                chunk_idx += 1

        logger.info(
            "Loss (no outliers): {}\tLoss (with outliers): {}".format(
                scipy.sparse.linalg.norm(v - self._W.dot(h)),
                scipy.sparse.linalg.norm(v - self._W.dot(h) - r),
            )
        )

    def _solve_w(self):
        """Update W matrix."""
        def error():
            return (
                0.5 * self._W.T.dot(self._W).dot(self.A).diagonal().sum()
                - self._W.T.dot(self.B).diagonal().sum()
            )

        eta = self._kappa / scipy.sparse.linalg.norm(self.A)

        if not self._w_error:
            self._w_error = error()

        for iter_number in range(self._w_max_iter):
            logger.debug("w_error: %s" % self._w_error)

            self._W -= eta * (self._W.dot(self.A) - self.B)
            self._transform()

            error_ = error()

            if (
                np.abs((error_ - self._w_error) / self._w_error)
                < self._w_stop_condition
            ):
                break

            self._w_error = error_

    def _apply(self, corpus, chunksize=None, **kwargs):
        """Apply the transformation to a whole corpus and get the result as another corpus.

        Parameters
        ----------
        corpus : iterable of list of (int, number)
            Corpus in sparse Gensim bag-of-words format.
        chunksize : int, optional
            If provided, a more effective processing will performed.

        Returns
        -------
        :class:`~gensim.interfaces.TransformedCorpus`
            Transformed corpus.

        """
        return TransformedCorpus(self, corpus, chunksize, **kwargs)

    @staticmethod
    def _solve_r(r, r_actual, lambda_, v_max):
        """Update residuals matrix.

        Parameters
        ----------
        r : scipy.sparse.csr_matrix
            Previous residuals.
        r_actual : scipy.sparse.csr_matrix(rshape)
            Actual residuals (v - Wh)
        lambda_ : float
            Regularization coefficient.
        v_max : float
            Residuals max and min boundary.

        Returns
        -------
        float
            Error between previous and current residuals.

        """
        r_actual.data *= np.abs(r_actual.data) > lambda_
        r_actual.eliminate_zeros()

        r_actual.data -= (r_actual.data > 0) * lambda_
        r_actual.data += (r_actual.data < 0) * lambda_

        np.clip(r_actual.data, -v_max, v_max, out=r_actual.data)

        violation = scipy.sparse.linalg.norm(r - r_actual)

        r.indices = r_actual.indices
        r.indptr = r_actual.indptr
        r.data = r_actual.data

        return violation

    def _transform(self):
        """Apply boundaries on W."""
        np.clip(self._W.data, 0, self.v_max, out=self._W.data)
        self._W.eliminate_zeros()
        sumsq = scipy.sparse.linalg.norm(self._W, axis=0)
        np.maximum(sumsq, 1, out=sumsq)
        sumsq = np.repeat(sumsq, self._W.getnnz(axis=0))
        self._W.data /= sumsq

        is_great_enough_data = self._W.data > self.w_std * self.sparse_coef
        is_great_enough = self._W.toarray() > self.w_std * self.sparse_coef
        is_all_too_small = is_great_enough.sum(axis=0) == 0
        is_all_too_small = np.repeat(is_all_too_small, self._W.getnnz(axis=0))

        is_great_enough_data |= is_all_too_small

        self._W.data *= is_great_enough_data
        self._W.eliminate_zeros()

    def _solveproj(self, v, W, h=None, r=None, v_max=None):
        """Update residuals and representation(h) matrices.

        Parameters
        ----------
        v : iterable of list(int, float)
            Subset of training corpus.
        W : scipy.sparse.csc_matrix
            Dictionary matrix.
        h : scipy.sparse.csr_matrix
            Representation matrix.
        r : scipy.sparse.csr_matrix
            Residuals matrix.
        v_max : float
            Maximum possible value in matrices.
        """
        m, n = W.shape
        if v_max is not None:
            self.v_max = v_max
        elif self.v_max is None:
            self.v_max = v.max()

        batch_size = v.shape[1]
        rshape = (m, batch_size)
        hshape = (n, batch_size)

        if h is None or h.shape != hshape:
            h = scipy.sparse.csr_matrix(hshape)

        if r is None or r.shape != rshape:
            r = scipy.sparse.csr_matrix(rshape)

        WtW = W.T.dot(W)

        _h_r_error = None

        for iter_number in range(self._h_r_max_iter):
            logger.debug("h_r_error: %s" % _h_r_error)

            error_ = 0

            Wt_v_minus_r = W.T.dot(v - r)

            h_ = h.toarray()
            error_ = max(
                error_, solve_h(h_, Wt_v_minus_r.toarray(), WtW.toarray(), self._kappa)
            )
            h = scipy.sparse.csr_matrix(h_)

            if self.use_r:
                r_actual = v - W.dot(h)
                error_ = max(
                    error_,
                    solve_r(
                        r.indptr,
                        r.indices,
                        r.data,
                        r_actual.indptr,
                        r_actual.indices,
                        r_actual.data,
                        self._lambda_,
                        self.v_max,
                    ),
                )
                r = r_actual

            error_ /= m

            if not _h_r_error:
                _h_r_error = error_
                continue

            if np.abs(_h_r_error - error_) < self._h_r_stop_condition:
                break

            _h_r_error = error_

        return h, r
