import io
import logging
import os
import pickle
import typing
from tqdm import tqdm
from typing import Any, Dict, List, Optional, Text, Tuple

import numpy as np

from scipy.sparse import issparse, find
from tensor2tensor.models.transformer import transformer_small, transformer_prepare_encoder, transformer_encoder
from tensor2tensor.layers.common_attention import add_timing_signal_1d

from rasa_nlu.classifiers import INTENT_RANKING_LENGTH
from rasa_nlu.components import Component

logger = logging.getLogger(__name__)

if typing.TYPE_CHECKING:
    import tensorflow as tf
    from rasa_nlu.config import RasaNLUModelConfig
    from rasa_nlu.training_data import TrainingData
    from rasa_nlu.model import Metadata
    from rasa_nlu.training_data import Message

try:
    import tensorflow as tf
except ImportError:
    tf = None


class EmbeddingIntentClassifier(Component):
    """Intent classifier using supervised embeddings.

    The embedding intent classifier embeds user inputs
    and intent labels into the same space.
    Supervised embeddings are trained by maximizing similarity between them.
    It also provides rankings of the labels that did not "win".

    The embedding intent classifier needs to be preceded by
    a featurizer in the pipeline.
    This featurizer creates the features used for the embeddings.
    It is recommended to use ``intent_featurizer_count_vectors`` that
    can be optionally preceded by ``nlp_spacy`` and ``tokenizer_spacy``.

    Based on the starspace idea from: https://arxiv.org/abs/1709.03856.
    However, in this implementation the `mu` parameter is treated differently
    and additional hidden layers are added together with dropout.
    """

    name = "intent_classifier_tensorflow_embedding"

    provides = ["intent", "intent_ranking"]

    requires = ["text_features"]

    defaults = {
        # nn architecture
        # sizes of hidden layers before the embedding layer for input words
        # the number of hidden layers is thus equal to the length of this list
        "hidden_layers_sizes_a": [256, 128],
        # sizes of hidden layers before the embedding layer for intent labels
        # the number of hidden layers is thus equal to the length of this list
        "hidden_layers_sizes_b": [],

        "share_embedding": False,
        "bidirectional": False,
        "fused_lstm": False,
        "gpu_lstm": False,
        "transformer": False,
        "pos_encoding": "timing",  # {"timing", "emb"}
        # introduce phase shift in time encodings between transformers
        # 0.5 - 0.8 works on small dataset
        "pos_max_timescale": 1.0e2,
        "max_seq_length": 256,
        "num_heads": 4,

        # training parameters
        "layer_norm": True,
        # initial and final batch sizes - batch size will be
        # linearly increased for each epoch
        "batch_size": [64, 256],
        # number of epochs
        "epochs": 300,

        # embedding parameters
        # dimension size of embedding vectors
        "embed_dim": 20,
        # how similar the algorithm should try
        # to make embedding vectors for correct intent labels
        "mu_pos": 0.8,  # should be 0.0 < ... < 1.0 for 'cosine'
        # maximum negative similarity for incorrect intent labels
        "mu_neg": -0.4,  # should be -1.0 < ... < 1.0 for 'cosine'
        # the type of the similarity
        "similarity_type": 'cosine',  # string 'cosine' or 'inner'
        # the number of incorrect intents, the algorithm will minimize
        # their similarity to the input words during training
        "num_neg": 20,
        "use_neg_from_batch": True,
        "use_iou": False,
        # flag: if true, only minimize the maximum similarity for
        # incorrect intent labels
        "use_max_sim_neg": True,
        # set random seed to any int to get reproducible results
        # try to change to another int if you are not getting good results
        "random_seed": None,

        # regularization parameters
        # the scale of L2 regularization
        "C2": 0.002,
        # the scale of how critical the algorithm should be of minimizing the
        # maximum similarity between embeddings of different intent labels
        "C_emb": 0.8,
        # dropout rate for rnn
        "droprate": 0.2,

        # flag: if true, the algorithm will split the intent labels into tokens
        #       and use bag-of-words representations for them
        "intent_tokenization_flag": False,
        # delimiter string to split the intent labels
        "intent_split_symbol": '_',

        # visualization of accuracy
        # how often to calculate training accuracy
        "evaluate_every_num_epochs": 10,  # small values may hurt performance
        # how many examples to use for calculation of training accuracy
        "evaluate_on_num_examples": 1000  # large values may hurt performance
    }

    def __init__(self,
                 component_config: Optional[Dict[Text, Any]] = None,
                 inv_intent_dict: Optional[Dict[int, Text]] = None,
                 encoded_all_intents: Optional[np.ndarray] = None,
                 all_intents_embed_values: Optional[np.ndarray] = None,
                 session: Optional['tf.Session'] = None,
                 graph: Optional['tf.Graph'] = None,
                 iterator=None,
                 message_placeholder: Optional['tf.Tensor'] = None,
                 intent_placeholder: Optional['tf.Tensor'] = None,
                 similarity_op: Optional['tf.Tensor'] = None,
                 all_intents_embed_in: Optional['tf.Tensor'] = None,
                 sim_all: Optional['tf.Tensor'] = None,
                 word_embed: Optional['tf.Tensor'] = None,
                 intent_embed: Optional['tf.Tensor'] = None
                 ) -> None:
        """Declare instant variables with default values"""

        self._check_tensorflow()
        super(EmbeddingIntentClassifier, self).__init__(component_config)

        self._load_params()

        # transform numbers to intents
        self.inv_intent_dict = inv_intent_dict
        # encode all intents with numbers
        self.encoded_all_intents = encoded_all_intents
        self.all_intents_embed_values = all_intents_embed_values
        self.iou = None

        # tf related instances
        self.session = session
        self.graph = graph
        self.iterator = iterator
        self.a_in = message_placeholder
        self.b_in = intent_placeholder
        self.sim_op = similarity_op

        self.all_intents_embed_in = all_intents_embed_in
        self.sim_all = sim_all

        self.sequence = len(self.a_in.shape) == 3 if self.a_in is not None else None

        # persisted embeddings
        self.word_embed = word_embed
        self.intent_embed = intent_embed

    # init helpers
    def _load_nn_architecture_params(self, config: Dict[Text, Any]) -> None:
        self.hidden_layer_sizes = {'a': config['hidden_layers_sizes_a'],
                                   'b': config['hidden_layers_sizes_b']}

        self.share_embedding = config['share_embedding']
        if self.share_embedding:
            if self.hidden_layer_sizes['a'] != self.hidden_layer_sizes['b']:
                raise ValueError("If embeddings are shared "
                                 "hidden_layer_sizes must coincide")

        self.bidirectional = config['bidirectional']
        self.fused_lstm = config['fused_lstm']
        self.gpu_lstm = config['gpu_lstm']
        self.transformer = config['transformer']
        if (self.gpu_lstm and self.fused_lstm) or (self.transformer and self.fused_lstm) or (self.gpu_lstm and self.transformer):
            raise ValueError("Either `gpu_lstm` or `fused_lstm` or `transformer` should be specified")
        if self.gpu_lstm or self.transformer:
            if any(self.hidden_layer_sizes['a'][0] != size
                   for size in self.hidden_layer_sizes['a']):
                raise ValueError("GPU training only supports identical sizes among layers a")
            if any(self.hidden_layer_sizes['b'][0] != size
                   for size in self.hidden_layer_sizes['b']):
                raise ValueError("GPU training only supports identical sizes among layers b")

        self.pos_encoding = config['pos_encoding']
        if self.pos_encoding == 'timing':
            self.pos_encoding = 'custom_timing'

        self.pos_max_timescale = config['pos_max_timescale']
        self.max_seq_length = config['max_seq_length']
        self.num_heads = config['num_heads']

        self.batch_size = config['batch_size']
        self.epochs = config['epochs']

    def _load_embedding_params(self, config: Dict[Text, Any]) -> None:
        self.layer_norm = config['layer_norm']
        self.embed_dim = config['embed_dim']
        self.mu_pos = config['mu_pos']
        self.mu_neg = config['mu_neg']
        self.similarity_type = config['similarity_type']
        self.num_neg = config['num_neg']
        self.use_neg_from_batch = config['use_neg_from_batch']
        self.use_iou = config['use_iou']
        self.use_max_sim_neg = config['use_max_sim_neg']
        self.random_seed = self.component_config['random_seed']

    def _load_regularization_params(self, config: Dict[Text, Any]) -> None:
        self.C2 = config['C2']
        self.C_emb = config['C_emb']
        self.droprate = config['droprate']

    def _load_flag_if_tokenize_intents(self, config: Dict[Text, Any]) -> None:
        self.intent_tokenization_flag = config['intent_tokenization_flag']
        self.intent_split_symbol = config['intent_split_symbol']
        if self.intent_tokenization_flag and not self.intent_split_symbol:
            logger.warning("intent_split_symbol was not specified, "
                           "so intent tokenization will be ignored")
            self.intent_tokenization_flag = False

    def _load_visual_params(self, config: Dict[Text, Any]) -> None:
        self.evaluate_every_num_epochs = config['evaluate_every_num_epochs']
        if self.evaluate_every_num_epochs < 1:
            self.evaluate_every_num_epochs = self.epochs

        self.evaluate_on_num_examples = config['evaluate_on_num_examples']
        if self.gpu_lstm:
            logger.info("Calculating train accuracy on gpu is not supported")
            self.evaluate_on_num_examples = 0

    def _load_params(self) -> None:

        self._load_nn_architecture_params(self.component_config)
        self._load_embedding_params(self.component_config)
        self._load_regularization_params(self.component_config)
        self._load_flag_if_tokenize_intents(self.component_config)
        self._load_visual_params(self.component_config)

    # package safety checks
    @classmethod
    def required_packages(cls) -> List[Text]:
        return ["tensorflow"]

    @staticmethod
    def _check_tensorflow():
        if tf is None:
            raise ImportError(
                'Failed to import `tensorflow`. '
                'Please install `tensorflow`. '
                'For example with `pip install tensorflow`.')

    # training data helpers:
    @staticmethod
    def _create_intent_dict(training_data: 'TrainingData') -> Dict[Text, int]:
        """Create intent dictionary"""

        distinct_intents = set([example.get("intent")
                                for example in training_data.intent_examples])
        return {intent: idx
                for idx, intent in enumerate(sorted(distinct_intents))}

    @staticmethod
    def _find_example_for_intent(intent, examples):
        for ex in examples:
            if ex.get("intent") == intent:
                return ex

    def _create_encoded_intents(self,
                                intent_dict: Dict[Text, int],
                                training_data: 'TrainingData') -> np.ndarray:
        """Create matrix with intents encoded in rows as bag of words.

        If intent_tokenization_flag is off, returns identity matrix.
        """

        if self.intent_tokenization_flag:
            encoded_all_intents = []

            for key, idx in intent_dict.items():
                encoded_all_intents.insert(
                    idx,
                    self._find_example_for_intent(
                        key,
                        training_data.intent_examples
                    ).get("intent_features")
                )

            return np.array(encoded_all_intents)
        else:
            return np.eye(len(intent_dict))

    # noinspection PyPep8Naming
    def _create_all_Y(self, size: int) -> np.ndarray:
        """Stack encoded_all_intents on top of each other

        to create candidates for training examples and
        to calculate training accuracy
        """

        return np.stack([self._toarray(self.encoded_all_intents)] * size)

    # noinspection PyPep8Naming
    def _prepare_data_for_training(
        self,
        training_data: 'TrainingData',
        intent_dict: Dict[Text, int]
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Prepare data for training"""

        X = np.stack([e.get("text_features")
                      for e in training_data.intent_examples])

        intents_for_X = np.array([intent_dict[e.get("intent")]
                                  for e in training_data.intent_examples])

        if self.intent_tokenization_flag:
            Y = np.stack([e.get("intent_features")
                          for e in training_data.intent_examples])
        else:
            Y = np.stack([self.encoded_all_intents[intent_idx]
                          for intent_idx in intents_for_X])

        return X, Y, intents_for_X

    # tf helpers:
    def _create_tf_embed_nn(self, x_in: 'tf.Tensor', is_training: 'tf.Tensor',
                            layer_sizes: List[int], name: Text) -> 'tf.Tensor':
        """Create nn with hidden layers and name"""

        reg = tf.contrib.layers.l2_regularizer(self.C2)
        x = x_in
        for i, layer_size in enumerate(layer_sizes):
            x = tf.layers.dense(inputs=x,
                                units=layer_size,
                                activation=tf.nn.relu,
                                kernel_regularizer=reg,
                                name='hidden_layer_{}_{}'.format(name, i),
                                reuse=tf.AUTO_REUSE)
            x = tf.layers.dropout(x, rate=self.droprate, training=is_training)

        x = tf.layers.dense(inputs=x,
                            units=self.embed_dim,
                            kernel_regularizer=reg,
                            name='embed_layer_{}'.format(name),
                            reuse=tf.AUTO_REUSE)
        return x

    def _create_rnn_cell(self,
                         is_training: 'tf.Tensor',
                         rnn_size: int,
                         real_length) -> 'tf.contrib.rnn.RNNCell':
        """Create one rnn cell."""

        # chrono initialization for forget bias
        # assuming that characteristic time is max dialogue length
        # left border that initializes forget gate close to 0
        bias_0 = -1.0
        characteristic_time = tf.reduce_mean(tf.cast(real_length, tf.float32))
        # right border that initializes forget gate close to 1
        bias_1 = tf.log(characteristic_time - 1.)
        fbias = (bias_1 - bias_0) * np.random.random(rnn_size) + bias_0

        keep_prob = 1.0 - (self.droprate *
                           tf.cast(is_training, tf.float32))

        return ChronoBiasLayerNormBasicLSTMCell(
            num_units=rnn_size,
            layer_norm=self.layer_norm,
            forget_bias=fbias,
            input_bias=-fbias,
            dropout_keep_prob=keep_prob,
            reuse=tf.AUTO_REUSE
        )

    def _create_tf_rnn_embed(self, x_in: 'tf.Tensor', is_training: 'tf.Tensor',
                             layer_sizes: List[int], name: Text) -> 'tf.Tensor':
        """Create rnn for dialogue level embedding."""

        reg = tf.contrib.layers.l2_regularizer(self.C2)
        # mask different length sequences
        mask = tf.sign(tf.reduce_max(x_in, -1))
        last = mask * tf.cumprod(1 - mask, axis=1, exclusive=True, reverse=True)
        mask = tf.cumsum(last, axis=1, reverse=True)
        real_length = tf.cast(tf.reduce_sum(mask, 1), tf.int32)

        last = tf.expand_dims(last, -1)

        x = tf.nn.relu(x_in)

        if self.fused_lstm:
            x = tf.transpose(x, [1, 0, 2])

            for i, layer_size in enumerate(layer_sizes):
                if self.bidirectional:
                    cell_fw = tf.contrib.rnn.LSTMBlockFusedCell(layer_size,
                                                                reuse=tf.AUTO_REUSE,
                                                                name='rnn_fw_encoder_{}_{}'.format(name, i))
                    x_fw, _ = cell_fw(x, dtype=tf.float32)

                    cell_bw = tf.contrib.rnn.LSTMBlockFusedCell(layer_size,
                                                                reuse=tf.AUTO_REUSE,
                                                                name='rnn_bw_encoder_{}_{}'.format(name, i))
                    x_bw, _ = cell_bw(tf.reverse_sequence(x, real_length, seq_axis=0, batch_axis=1), dtype=tf.float32)

                    x = tf.concat([x_fw, x_bw], -1)

                else:
                    cell = tf.contrib.rnn.LSTMBlockFusedCell(layer_size,
                                                             reuse=tf.AUTO_REUSE,
                                                             name='rnn_encoder_{}_{}'.format(name, i))
                    x, _ = cell(x, dtype=tf.float32)

            x = tf.transpose(x, [1, 0, 2])
            x = tf.reduce_sum(x * last, 1)

        elif self.gpu_lstm:
            # only trains and predicts on gpu_lstm
            x = tf.transpose(x, [1, 0, 2])

            if self.bidirectional:
                direction = 'bidirectional'
            else:
                direction = 'unidirectional'

            lstm = tf.contrib.cudnn_rnn.CudnnLSTM(len(layer_sizes),
                                                  layer_sizes[0],
                                                  direction=direction,
                                                  name='rnn_encoder_{}'.format(name))

            x, _ = lstm(x, training=True)
            # prediction graph is created separately

            x = tf.transpose(x, [1, 0, 2])
            x = tf.reduce_sum(x * last, 1)

        elif self.transformer:
            hparams = transformer_small()

            hparams.num_hidden_layers = len(layer_sizes)
            hparams.hidden_size = layer_sizes[0]
            # it seems to be factor of 4 for transformer architectures in t2t
            hparams.filter_size = layer_sizes[0] * 4
            hparams.num_heads = self.num_heads
            # hparams.relu_dropout = self.droprate
            hparams.pos = self.pos_encoding

            hparams.max_length = self.max_seq_length
            if not self.bidirectional:
                hparams.unidirectional_encoder = True

            # When not in training mode, set all forms of dropout to zero.
            for key, value in hparams.values().items():
                if key.endswith("dropout") or key == "label_smoothing":
                    setattr(hparams, key, value * tf.cast(is_training, tf.float32))

            x = tf.layers.dense(inputs=x,
                                units=hparams.hidden_size,
                                use_bias=False,
                                kernel_initializer=tf.random_normal_initializer(0.0, hparams.hidden_size**-0.5),
                                kernel_regularizer=reg,
                                name='transformer_embed_layer_{}'.format(name),
                                reuse=tf.AUTO_REUSE)
            x = tf.layers.dropout(x, rate=hparams.layer_prepostprocess_dropout, training=is_training)

            if hparams.multiply_embedding_mode == "sqrt_depth":
                x *= hparams.hidden_size**0.5

            x *= tf.expand_dims(mask, -1)

            with tf.variable_scope('transformer_{}'.format(name), reuse=tf.AUTO_REUSE):
                (x,
                 self_attention_bias,
                 encoder_decoder_attention_bias
                 ) = transformer_prepare_encoder(x, None, hparams)

                if hparams.pos == 'custom_timing':
                    x = add_timing_signal_1d(x, max_timescale=self.pos_max_timescale)

                x *= tf.expand_dims(mask, -1)

                x = tf.nn.dropout(x, 1.0 - hparams.layer_prepostprocess_dropout)

                attn_bias_for_padding = None
                # Otherwise the encoder will just use encoder_self_attention_bias.
                if hparams.unidirectional_encoder:
                    attn_bias_for_padding = encoder_decoder_attention_bias

                x = transformer_encoder(
                    x,
                    self_attention_bias,
                    hparams,
                    nonpadding=mask,
                    attn_bias_for_padding=attn_bias_for_padding)

            # x = tf.reduce_sum(x * last, 1)
            x = tf.reduce_mean(x, 1)

        else:
            for i, layer_size in enumerate(layer_sizes):
                if self.bidirectional:
                    cell_fw = self._create_rnn_cell(is_training, layer_size, real_length)
                    cell_bw = self._create_rnn_cell(is_training, layer_size, real_length)

                    x, _ = tf.nn.bidirectional_dynamic_rnn(
                        cell_fw, cell_bw, x,
                        dtype=tf.float32,
                        sequence_length=real_length,
                        scope='rnn_encoder_{}_{}'.format(name, i)
                    )
                    x = tf.concat(x, 2)

                else:
                    cell = self._create_rnn_cell(is_training, layer_size, real_length)

                    x, _ = tf.nn.dynamic_rnn(
                        cell, x,
                        dtype=tf.float32,
                        sequence_length=real_length,
                        scope='rnn_encoder_{}_{}'.format(name, i)
                    )

            x = tf.reduce_sum(x * last, 1)

        return tf.layers.dense(inputs=x,
                               units=self.embed_dim,
                               kernel_regularizer=reg,
                               name='embed_layer_{}'.format(name),
                               reuse=tf.AUTO_REUSE)

    def _create_tf_embed_a(self,
                           a_in: 'tf.Tensor',
                           is_training: 'tf.Tensor',
                           ) -> 'tf.Tensor':
        """Create tf graph for training"""

        if len(a_in.shape) == 2:
            emb_a = self._create_tf_embed_nn(a_in, is_training,
                                             self.hidden_layer_sizes['a'],
                                             name='a_and_b' if self.share_embedding else 'a')
        else:
            emb_a = self._create_tf_rnn_embed(a_in, is_training,
                                              self.hidden_layer_sizes['a'],
                                              name='a_and_b' if self.share_embedding else 'a')

        return emb_a

    def _create_tf_embed_b(self,
                           b_in: 'tf.Tensor',
                           is_training: 'tf.Tensor',
                           ) -> 'tf.Tensor':
        """Create tf graph for training"""

        # if len(b_in.shape) == 3:
        #     emb_b = self._create_tf_embed_nn(b_in, is_training,
        #                                      self.hidden_layer_sizes['b'],
        #                                      name='a_and_b' if self.share_embedding else 'b')
        #
        # else:
        #     # reshape b_in
        #     shape = tf.shape(b_in)
        #     b_in = tf.reshape(b_in, [-1, shape[-2], b_in.shape[-1]])
        emb_b = self._create_tf_rnn_embed(b_in, is_training,
                                          self.hidden_layer_sizes['b'],
                                          name='a_and_b' if self.share_embedding else 'b')
            # reshape back
            # emb_b = tf.reshape(emb_b, [shape[0], shape[1], self.embed_dim])

        return emb_b

    @staticmethod
    def _tf_sample_neg(emb_b,
                       is_training: 'tf.Tensor',
                       neg_ids,
                       ) -> 'tf.Tensor':

        # all_b = emb_b[tf.newaxis, :, 0, :]
        all_b = emb_b[tf.newaxis, :, :]
        all_b = tf.tile(all_b, [tf.shape(emb_b)[0], 1, 1])

        def sample_neg_b():
            neg_b = tf.batch_gather(all_b, neg_ids)
            return tf.concat([emb_b[:, tf.newaxis, :], neg_b], 1)

        emb_b = tf.cond(tf.logical_and(is_training, tf.shape(neg_ids)[0] > 1), sample_neg_b, lambda: all_b)

        return emb_b

    def _tf_sim(self,
                a: 'tf.Tensor',
                b: 'tf.Tensor') -> Tuple['tf.Tensor', 'tf.Tensor']:
        """Define similarity

        in two cases:
            sim: between embedded words and embedded intent labels
            sim_emb: between individual embedded intent labels only
        """

        if self.similarity_type == 'cosine':
            # normalize embedding vectors for cosine similarity
            a = tf.nn.l2_normalize(a, -1)
            b = tf.nn.l2_normalize(b, -1)

        if self.similarity_type in {'cosine', 'inner'}:
            sim = tf.reduce_sum(tf.expand_dims(a, 1) * b, -1)
            sim_emb = tf.reduce_sum(b[:, 0:1, :] * b[:, 1:, :], -1)

            return sim, sim_emb

        else:
            raise ValueError("Wrong similarity type {}, "
                             "should be 'cosine' or 'inner'"
                             "".format(self.similarity_type))

    def _tf_loss(self, sim: 'tf.Tensor', sim_emb: 'tf.Tensor') -> 'tf.Tensor':
        """Define loss"""

        # loss for maximizing similarity with correct action
        loss = tf.maximum(0., self.mu_pos - sim[:, 0])

        if self.use_max_sim_neg:
            # minimize only maximum similarity over incorrect actions
            max_sim_neg = tf.reduce_max(sim[:, 1:], -1)
            loss += tf.maximum(0., self.mu_neg + max_sim_neg)
        else:
            # minimize all similarities with incorrect actions
            max_margin = tf.maximum(0., self.mu_neg + sim[:, 1:])
            loss += tf.reduce_sum(max_margin, -1)

        # penalize max similarity between intent embeddings
        max_sim_emb = tf.maximum(0., tf.reduce_max(sim_emb, -1))
        loss += max_sim_emb * self.C_emb

        # average the loss over the batch and add regularization losses
        loss = (tf.reduce_mean(loss) + tf.losses.get_regularization_loss())
        return loss

    # training helpers:
    def _create_batch_b(self, batch_pos_b: np.ndarray,
                        intent_ids: np.ndarray) -> np.ndarray:
        """Create batch of intents.

        Where the first is correct intent
        and the rest are wrong intents sampled randomly
        """

        batch_pos_b = np.expand_dims(batch_pos_b, axis=1)

        # sample negatives
        if len(batch_pos_b.shape) == 3:
            batch_neg_b = np.zeros((batch_pos_b.shape[0], self.num_neg,
                                    batch_pos_b.shape[-1]))
        else:
            batch_neg_b = np.zeros((batch_pos_b.shape[0], self.num_neg,
                                    batch_pos_b.shape[-2], batch_pos_b.shape[-1]))

        for b in range(batch_pos_b.shape[0]):
            # create negative indexes out of possible ones
            # except for correct index of b
            if self.use_iou:
                negative_indexes = [i for i in
                                    range(self.encoded_all_intents.shape[0])
                                    if self.iou[i, intent_ids[b]] < 0.66]
            else:
                negative_indexes = [i for i in
                                    range(self.encoded_all_intents.shape[0])
                                    if i != intent_ids[b]]
            negs = np.random.choice(negative_indexes, size=self.num_neg)

            batch_neg_b[b] = self._toarray(self.encoded_all_intents[negs])

        return np.concatenate([batch_pos_b, batch_neg_b], 1)

    def _negs_from_batch(self, batch_pos_b: np.ndarray,
                         intent_ids: np.ndarray) -> np.ndarray:
        """Find incorrect intents in the batch."""

        negs = []
        for b in range(batch_pos_b.shape[0]):
            # create negative indexes out of possible ones
            # except for correct index of b
            if self.use_iou:
                negative_indexes = [i for i in
                                    range(batch_pos_b.shape[0])
                                    if self.iou[intent_ids[i], intent_ids[b]] < 0.66]
            else:
                negative_indexes = [i for i in
                                    range(batch_pos_b.shape[0])
                                    if not np.array_equal(batch_pos_b[i], batch_pos_b[b])]

            negs_ids = np.random.choice(negative_indexes, size=self.num_neg)
            negs.append(np.eye(batch_pos_b.shape[0])[negs_ids])

        return np.array(negs)

    def _linearly_increasing_batch_size(self, epoch: int) -> int:
        """Linearly increase batch size with every epoch.

        The idea comes from https://arxiv.org/abs/1711.00489
        """

        if not isinstance(self.batch_size, list):
            return int(self.batch_size)

        if self.epochs > 1:
            batch_size = int(self.batch_size[0] +
                             epoch * (self.batch_size[1] -
                                      self.batch_size[0]) / (self.epochs - 1))

            return batch_size if batch_size % 2 == 0 else batch_size + 1

        else:
            return int(self.batch_size[0])

    def _toarray(self, array_of_sparse):
        if issparse(array_of_sparse):
            return array_of_sparse.toarray()
        elif issparse(array_of_sparse[0]):
            if not self.sequence:
                return np.array([x.toarray() for x in array_of_sparse]).squeeze()
            else:
                seq_len = max([x.shape[0] for x in array_of_sparse])
                X = np.ones([len(array_of_sparse), seq_len, array_of_sparse[0].shape[-1]], dtype=np.int32) * -1
                for i, x in enumerate(array_of_sparse):
                    X[i, :x.shape[0], :] = x.toarray()

                return X
        else:
            return array_of_sparse

    # noinspection PyPep8Naming
    def _train_tf(self,
                  X: np.ndarray,
                  Y: np.ndarray,
                  intents_for_X: np.ndarray,
                  negs_in,
                  loss: 'tf.Tensor',
                  is_training: 'tf.Tensor',
                  train_op: 'tf.Tensor'
                  ) -> None:
        """Train tf graph"""

        self.session.run(tf.global_variables_initializer())

        if self.evaluate_on_num_examples:
            logger.info("Accuracy is updated every {} epochs"
                        "".format(self.evaluate_every_num_epochs))

        pbar = tqdm(range(self.epochs), desc="Epochs")
        train_acc = 0
        last_loss = 0
        for ep in pbar:
            indices = np.random.permutation(len(X))

            batch_size = self._linearly_increasing_batch_size(ep)
            batches_per_epoch = (len(X) // batch_size +
                                 int(len(X) % batch_size > 0))

            ep_loss = 0
            for i in range(batches_per_epoch):
                end_idx = (i + 1) * batch_size
                start_idx = i * batch_size

                batch_a = X[indices[start_idx:end_idx]]
                if batch_a.shape[0] % 2 != 0:
                    start_idx -= 1
                batch_a = self._toarray(X[indices[start_idx:end_idx]])

                batch_pos_b = self._toarray(Y[indices[start_idx:end_idx]])
                intents_for_b = self._toarray(intents_for_X[indices[start_idx:end_idx]])
                # add negatives
                if self.use_neg_from_batch:
                    negs = self._negs_from_batch(batch_pos_b, intents_for_b)
                    batch_b = np.expand_dims(batch_pos_b, axis=1)
                    sess_out = self.session.run(
                        {'loss': loss, 'train_op': train_op},
                        feed_dict={self.a_in: batch_a,
                                   self.b_in: batch_b,
                                   negs_in: negs,
                                   is_training: True}
                    )
                else:
                    batch_b = self._create_batch_b(batch_pos_b, intents_for_b)

                    sess_out = self.session.run(
                        {'loss': loss, 'train_op': train_op},
                        feed_dict={self.a_in: batch_a,
                                   self.b_in: batch_b,
                                   is_training: True}
                    )
                ep_loss += sess_out.get('loss') / batches_per_epoch

            if self.evaluate_on_num_examples:
                if (ep == 0 or
                        (ep + 1) % self.evaluate_every_num_epochs == 0 or
                        (ep + 1) == self.epochs):
                    train_acc = self._output_training_stat(X, Y, intents_for_X,
                                                           is_training)
                    last_loss = ep_loss

                pbar.set_postfix({
                    "loss": "{:.3f}".format(ep_loss),
                    "acc": "{:.3f}".format(train_acc)
                })
            else:
                pbar.set_postfix({
                    "loss": "{:.3f}".format(ep_loss)
                })

        if self.evaluate_on_num_examples:
            logger.info("Finished training embedding classifier, "
                        "loss={:.3f}, train accuracy={:.3f}"
                        "".format(last_loss, train_acc))

    # noinspection PyPep8Naming
    def _output_training_stat(self,
                              X: np.ndarray,
                              Y: np.ndarray,
                              intents_for_X: np.ndarray,
                              is_training: 'tf.Tensor') -> np.ndarray:
        """Output training statistics"""

        n = self.evaluate_on_num_examples
        ids = np.random.permutation(len(X))[:n]
        if self.use_neg_from_batch:
            all_Y = np.expand_dims(self._toarray(Y[ids]), axis=1)
        else:
            all_Y = self._create_all_Y(X[ids].shape[0])

        train_sim = self.session.run(self.sim_op,
                                     feed_dict={self.a_in: self._toarray(X[ids]),
                                                self.b_in: all_Y,
                                                is_training: False})
        if self.use_neg_from_batch:
            train_acc = np.mean(np.argmax(train_sim, -1) == np.arange(n))
        else:
            train_acc = np.mean(np.argmax(train_sim, -1) == intents_for_X[ids])

        return train_acc

    @staticmethod
    def _to_sparse_tensor(array_of_sparse, seq_len):
        coo = [x.tocoo() for x in array_of_sparse]
        indices = [ids for i, x in enumerate(coo) for ids in zip([i] * len(x.row), x.row, x.col)]
        data = [v for x in array_of_sparse for v in x.data]
        return tf.SparseTensor(indices, data, (len(array_of_sparse), seq_len, array_of_sparse[0].shape[-1]))

    # noinspection PyPep8Naming
    def train(self,
              training_data: 'TrainingData',
              cfg: Optional['RasaNLUModelConfig'] = None,
              **kwargs: Any) -> None:
        """Train the embedding intent classifier on a data set."""

        intent_dict = self._create_intent_dict(training_data)
        if len(intent_dict) < 2:
            logger.error("Can not train an intent classifier. "
                         "Need at least 2 different classes. "
                         "Skipping training of intent classifier.")
            return

        self.inv_intent_dict = {v: k for k, v in intent_dict.items()}
        self.encoded_all_intents = self._create_encoded_intents(
            intent_dict, training_data)

        X, Y, intents_for_X = self._prepare_data_for_training(
            training_data, intent_dict)

        self.sequence = len(X.shape) != 2 and any(x.shape[0] != 1 for x in X)

        if self.share_embedding:
            if X[0].shape[-1] != Y[0].shape[-1]:
                raise ValueError("If embeddings are shared "
                                 "text features and intent features "
                                 "must coincide")

        # check if number of negatives is less than number of intents
        logger.debug("Check if num_neg {} is smaller than "
                     "number of intents {}, "
                     "else set num_neg to the number of intents - 1"
                     "".format(self.num_neg,
                               self.encoded_all_intents.shape[0]))
        self.num_neg = min(self.num_neg,
                           self.encoded_all_intents.shape[0] - 1)

        self.graph = tf.Graph()
        with self.graph.as_default():
            # set random seed
            np.random.seed(self.random_seed)
            tf.set_random_seed(self.random_seed)

            X_seq_len = max([x.shape[0] for x in X])
            Y_seq_len = max([y.shape[0] for y in Y])
            X_tensor = self._to_sparse_tensor(X, X_seq_len)
            Y_tensor = self._to_sparse_tensor(Y, Y_seq_len)

            batch_size_in = tf.placeholder(tf.int64)
            train_dataset = tf.data.Dataset.from_tensor_slices((X_tensor, Y_tensor))
            train_dataset = train_dataset.shuffle(buffer_size=len(X))
            train_dataset = train_dataset.batch(batch_size_in, drop_remainder=self.fused_lstm)

            if self.evaluate_on_num_examples:
                ids = np.random.permutation(len(X))[:self.evaluate_on_num_examples]
                X_tensor_val = self._to_sparse_tensor(X[ids], X_seq_len)
                Y_tensor_val = self._to_sparse_tensor(Y[ids], Y_seq_len)

                val_dataset = tf.data.Dataset.from_tensor_slices((X_tensor_val, Y_tensor_val)).batch(self.evaluate_on_num_examples)
            else:
                val_dataset = None

            self.iterator = tf.data.Iterator.from_structure(train_dataset.output_types,
                                                            train_dataset.output_shapes,
                                                            output_classes=train_dataset.output_classes)
            # iterator = train_dataset.make_initializable_iterator()
            a_sparse, b_sparse = self.iterator.get_next()

            self.a_in = tf.cast(tf.reshape(tf.sparse_tensor_to_dense(a_sparse),
                                           (tf.shape(a_sparse)[0], tf.shape(a_sparse)[1], X_tensor.shape[2])),
                                tf.float32)
            self.b_in = tf.cast(tf.reshape(tf.sparse_tensor_to_dense(b_sparse),
                                           (tf.shape(b_sparse)[0], tf.shape(b_sparse)[1], Y_tensor.shape[2])),
                                tf.float32)

            is_training = tf.placeholder_with_default(False, shape=())

            self.word_embed = self._create_tf_embed_a(self.a_in, is_training)
            self.intent_embed = self._create_tf_embed_b(self.b_in, is_training)

            negs_ids = tf.random.categorical(tf.log(1. - tf.eye(tf.shape(self.b_in)[0])), 3)  # self.num_neg)

            # tiled_intent_raw = self._tf_sample_neg(tf.reduce_sum(self.b_in, 1), is_training, negs_ids)
            #
            # train_init_op = self.iterator.make_initializer(train_dataset)
            #
            # self.session = tf.Session()
            #
            # self.session.run(train_init_op, feed_dict={batch_size_in: 4})
            #
            # x = self.session.run(tiled_intent_raw)
            # print(x[0, 0])
            # print(x[0, 1])
            # exit()

            tiled_intent_embed = self._tf_sample_neg(self.intent_embed, is_training, negs_ids)

            self.sim_op, sim_emb = self._tf_sim(self.word_embed,
                                                tiled_intent_embed)
            loss = self._tf_loss(self.sim_op, sim_emb)

            train_op = tf.train.AdamOptimizer().minimize(loss)

            train_init_op = self.iterator.make_initializer(train_dataset)
            val_init_op = self.iterator.make_initializer(val_dataset)

            # [print(v.name, v.shape) for v in tf.trainable_variables()]
            # exit()
            # train tensorflow graph
            self.session = tf.Session()

            # self._train_tf(X, Y, intents_for_X, negs_in,
            #                loss, is_training, train_op)
            self._train_tf_dataset(train_init_op, val_init_op, batch_size_in, loss, is_training, train_op)
            exit()
            self.all_intents_embed_values = self._create_all_intents_embed(self.encoded_all_intents)

            self.all_intents_embed_in = tf.placeholder(tf.float32, (None, None, self.embed_dim),
                                                       name='all_intents_embed')

            self.sim_all, _ = self._tf_sim(self.word_embed, self.all_intents_embed_in)

    # noinspection PyPep8Naming
    def _train_tf_dataset(self,
                          train_init_op,
                          val_init_op,
                          batch_size_in,
                          loss: 'tf.Tensor',
                          is_training: 'tf.Tensor',
                          train_op: 'tf.Tensor'
                          ) -> None:
        """Train tf graph"""

        self.session.run(tf.global_variables_initializer())

        if self.evaluate_on_num_examples:
            logger.info("Accuracy is updated every {} epochs"
                        "".format(self.evaluate_every_num_epochs))

        pbar = tqdm(range(self.epochs), desc="Epochs")
        train_acc = 0
        last_loss = 0
        for ep in pbar:

            batch_size = self._linearly_increasing_batch_size(ep)
            self.session.run(train_init_op, feed_dict={batch_size_in: batch_size})

            ep_loss = 0
            batches_per_epoch = 0
            while True:
                try:
                    _, batch_loss = self.session.run((train_op, loss),
                                                     feed_dict={is_training: True})
                except tf.errors.OutOfRangeError:
                    break

                batches_per_epoch += 1
                ep_loss += batch_loss

            ep_loss /= batches_per_epoch

            if self.evaluate_on_num_examples:
                if (ep == 0 or
                        (ep + 1) % self.evaluate_every_num_epochs == 0 or
                        (ep + 1) == self.epochs):
                    train_acc = self._output_training_stat_dataset(val_init_op)
                    last_loss = ep_loss

                pbar.set_postfix({
                    "loss": "{:.3f}".format(ep_loss),
                    "acc": "{:.3f}".format(train_acc)
                })
            else:
                pbar.set_postfix({
                    "loss": "{:.3f}".format(ep_loss)
                })

        if self.evaluate_on_num_examples:
            logger.info("Finished training embedding classifier, "
                        "loss={:.3f}, train accuracy={:.3f}"
                        "".format(last_loss, train_acc))

    # noinspection PyPep8Naming
    def _output_training_stat_dataset(self, val_init_op) -> np.ndarray:
        """Output training statistics"""

        self.session.run(val_init_op)
        train_sim = self.session.run(self.sim_op)

        train_acc = np.mean(np.argmax(train_sim, -1) == np.arange(len(train_sim)))

        return train_acc

    def _create_all_intents_embed(self, encoded_all_intents):
        batch_size = self._linearly_increasing_batch_size(0)
        batches_per_epoch = (len(encoded_all_intents) // batch_size +
                             int(len(encoded_all_intents) % batch_size > 0))

        all_intents_embed = np.empty((1, len(encoded_all_intents), self.embed_dim))
        for i in range(batches_per_epoch):
            end_idx = (i + 1) * batch_size
            start_idx = i * batch_size

            batch_b = self._toarray(encoded_all_intents[start_idx:end_idx])
            batch_b = np.expand_dims(batch_b, axis=0)

            all_intents_embed[
                0, start_idx:end_idx, :
            ] = self.session.run(self.intent_embed, feed_dict={self.b_in: batch_b})

        return all_intents_embed

    # process helpers
    # noinspection PyPep8Naming
    def _calculate_message_sim(self,
                               X: np.ndarray,
                               all_Y: np.ndarray
                               ) -> Tuple[np.ndarray, List[float]]:
        """Load tf graph and calculate message similarities"""

        message_sim = self.session.run(self.sim_op,
                                       feed_dict={self.a_in: X,
                                                  self.b_in: all_Y})
        message_sim = message_sim.flatten()  # sim is a matrix

        intent_ids = message_sim.argsort()[::-1]
        message_sim[::-1].sort()

        if self.similarity_type == 'cosine':
            # clip negative values to zero
            message_sim[message_sim < 0] = 0
        elif self.similarity_type == 'inner':
            # normalize result to [0, 1] with softmax
            message_sim = np.exp(message_sim)
            message_sim /= np.sum(message_sim)

        # transform sim to python list for JSON serializing
        return intent_ids, message_sim.tolist()

    # noinspection PyPep8Naming
    def _calculate_message_sim_all(self,
                                   X: np.ndarray
                                   ) -> Tuple[np.ndarray, List[float]]:
        """Load tf graph and calculate message similarities"""

        message_sim = self.session.run(
            self.sim_all,
            feed_dict={self.a_in: X,
                       self.all_intents_embed_in: self.all_intents_embed_values}
        )
        message_sim = message_sim.flatten()  # sim is a matrix

        intent_ids = message_sim.argsort()[::-1]
        message_sim[::-1].sort()

        if self.similarity_type == 'cosine':
            # clip negative values to zero
            message_sim[message_sim < 0] = 0
        elif self.similarity_type == 'inner':
            # normalize result to [0, 1] with softmax
            message_sim = np.exp(message_sim)
            message_sim /= np.sum(message_sim)

        # transform sim to python list for JSON serializing
        return intent_ids, message_sim.tolist()

    # noinspection PyPep8Naming
    def process(self, message: 'Message', **kwargs: Any) -> None:
        """Return the most likely intent and its similarity to the input."""

        intent = {"name": None, "confidence": 0.0}
        intent_ranking = []

        if self.session is None:
            logger.error("There is no trained tf.session: "
                         "component is either not trained or "
                         "didn't receive enough training data")

        else:
            # get features (bag of words) for a message
            X = message.get("text_features")
            if issparse(X[0]):
                X = self._toarray(X)
            else:
                X = np.expand_dims(message.get("text_features"), axis=0)

            # stack encoded_all_intents on top of each other
            # to create candidates for test examples
            # all_Y = self._create_all_Y(X.shape[0])

            # load tf graph and session
            # intent_ids, message_sim = self._calculate_message_sim(X, all_Y)

            intent_ids, message_sim = self._calculate_message_sim_all(X)

            # if X contains all zeros do not predict some label
            if X.any() and intent_ids.size > 0:
                intent = {"name": self.inv_intent_dict[intent_ids[0]],
                          "confidence": message_sim[0]}

                ranking = list(zip(list(intent_ids), message_sim))
                ranking = ranking[:INTENT_RANKING_LENGTH]
                intent_ranking = [{"name": self.inv_intent_dict[intent_idx],
                                   "confidence": score}
                                  for intent_idx, score in ranking]

        message.set("intent", intent, add_to_output=True)
        message.set("intent_ranking", intent_ranking, add_to_output=True)

    def persist(self, model_dir: Text) -> Dict[Text, Any]:
        """Persist this model into the passed directory.

        Return the metadata necessary to load the model again.
        """

        if self.session is None:
            return {"classifier_file": None}

        checkpoint = os.path.join(model_dir, self.name + ".ckpt")

        try:
            os.makedirs(os.path.dirname(checkpoint))
        except OSError as e:
            # be happy if someone already created the path
            import errno
            if e.errno != errno.EEXIST:
                raise
        with self.graph.as_default():
            if not self.gpu_lstm:
                self.graph.clear_collection('message_placeholder')
                self.graph.add_to_collection('message_placeholder',
                                             self.a_in)

                self.graph.clear_collection('intent_placeholder')
                self.graph.add_to_collection('intent_placeholder',
                                             self.b_in)

                self.graph.clear_collection('similarity_op')
                self.graph.add_to_collection('similarity_op',
                                             self.sim_op)

                self.graph.clear_collection('all_intents_embed_in')
                self.graph.add_to_collection('all_intents_embed_in',
                                             self.all_intents_embed_in)
                self.graph.clear_collection('sim_all')
                self.graph.add_to_collection('sim_all',
                                             self.sim_all)

                self.graph.clear_collection('word_embed')
                self.graph.add_to_collection('word_embed',
                                             self.word_embed)
                self.graph.clear_collection('intent_embed')
                self.graph.add_to_collection('intent_embed',
                                             self.intent_embed)

            saver = tf.train.Saver()
            saver.save(self.session, checkpoint)

        placeholder_dims = {'a_in': np.int(self.a_in.shape[-1]),
                            'b_in': np.int(self.b_in.shape[-1])}
        with io.open(os.path.join(
                model_dir,
                self.name + "_placeholder_dims.pkl"), 'wb') as f:
            pickle.dump(placeholder_dims, f)
        with io.open(os.path.join(
                model_dir,
                self.name + "_inv_intent_dict.pkl"), 'wb') as f:
            pickle.dump(self.inv_intent_dict, f)
        with io.open(os.path.join(
                model_dir,
                self.name + "_encoded_all_intents.pkl"), 'wb') as f:
            pickle.dump(self.encoded_all_intents, f)
        with io.open(os.path.join(
                model_dir,
                self.name + "_all_intents_embed_values.pkl"), 'wb') as f:
            pickle.dump(self.all_intents_embed_values, f)

        return {"classifier_file": self.name + ".ckpt"}

    @staticmethod
    def _create_tf_gpu_predict_embed(meta, x_in: 'tf.Tensor',
                                     layer_sizes: List[int], name: Text) -> 'tf.Tensor':
        """Used for prediction if gpu_lstm is true"""

        reg = tf.contrib.layers.l2_regularizer(meta['C2'])
        # mask different length sequences
        # if there is at least one `-1` it should be masked
        mask = tf.sign(tf.reduce_max(x_in, -1) + 1)
        last = tf.expand_dims(mask * tf.cumprod(1 - mask, axis=1, exclusive=True, reverse=True), -1)
        real_length = tf.cast(tf.reduce_sum(mask, 1), tf.int32)

        x = tf.nn.relu(x_in)

        if meta['bidirectional']:
            with tf.variable_scope('rnn_encoder_{}'.format(name)):
                single_cell = lambda: tf.contrib.cudnn_rnn.CudnnCompatibleLSTMCell(layer_sizes[0], reuse=tf.AUTO_REUSE)
                cells_fw = [single_cell() for _ in range(len(layer_sizes))]
                cells_bw = [single_cell() for _ in range(len(layer_sizes))]
                x, _, _ = tf.contrib.rnn.stack_bidirectional_dynamic_rnn(cells_fw, cells_bw, x,
                                                                         dtype=tf.float32,
                                                                         sequence_length=real_length)
        else:
            with tf.variable_scope('rnn_encoder_{}'.format(name)):
                single_cell = lambda: tf.contrib.cudnn_rnn.CudnnCompatibleLSTMCell(layer_sizes[0], reuse=tf.AUTO_REUSE)
                # NOTE: Even if there's only one layer, the cell needs to be wrapped in
                # MultiRNNCell.
                cell = tf.nn.rnn_cell.MultiRNNCell([single_cell() for _ in range(len(layer_sizes))])
                # Leave the scope arg unset.
                x, _ = tf.nn.dynamic_rnn(cell, x, dtype=tf.float32, sequence_length=real_length)

        x = tf.reduce_sum(x * last, 1)

        return tf.layers.dense(inputs=x,
                               units=meta['embed_dim'],
                               kernel_regularizer=reg,
                               name='embed_layer_{}'.format(name),
                               reuse=tf.AUTO_REUSE)

    @staticmethod
    def _tf_gpu_sim(meta,
                    a: 'tf.Tensor',
                    b: 'tf.Tensor') -> Tuple['tf.Tensor', 'tf.Tensor']:
        """Define similarity

        in two cases:
            sim: between embedded words and embedded intent labels
            sim_emb: between individual embedded intent labels only
        """

        if meta['similarity_type'] == 'cosine':
            # normalize embedding vectors for cosine similarity
            a = tf.nn.l2_normalize(a, -1)
            b = tf.nn.l2_normalize(b, -1)

        if meta['similarity_type'] in {'cosine', 'inner'}:
            sim = tf.reduce_sum(tf.expand_dims(a, 1) * b, -1)
            sim_emb = tf.reduce_sum(b[:, 0:1, :] * b[:, 1:, :], -1)

            return sim, sim_emb

        else:
            raise ValueError("Wrong similarity type {}, "
                             "should be 'cosine' or 'inner'"
                             "".format(meta['similarity_type']))

    @classmethod
    def load(cls,
             model_dir: Text = None,
             model_metadata: 'Metadata' = None,
             cached_component: Optional['EmbeddingIntentClassifier'] = None,
             **kwargs: Any
             ) -> 'EmbeddingIntentClassifier':

        meta = model_metadata.for_component(cls.name)

        if model_dir and meta.get("classifier_file"):
            file_name = meta.get("classifier_file")
            checkpoint = os.path.join(model_dir, file_name)

            graph = tf.Graph()
            with graph.as_default():
                sess = tf.Session()
                if meta['gpu_lstm']:
                    # rebuild tf graph for prediction
                    with io.open(os.path.join(
                            model_dir,
                            cls.name + "_placeholder_dims.pkl"), 'rb') as f:
                        placeholder_dims = pickle.load(f)
                    a_in = tf.placeholder(tf.float32, (None, None, placeholder_dims['a_in']),
                                          name='a')
                    b_in = tf.placeholder(tf.float32, (None, None, None, placeholder_dims['b_in']),
                                          name='b')
                    word_embed = cls._create_tf_gpu_predict_embed(meta, a_in,
                                                                  meta['hidden_layers_sizes_a'],
                                                                  name='a_and_b' if meta['share_embedding'] else 'a')
                    shape = tf.shape(b_in)
                    b = tf.reshape(b_in, [-1, shape[-2], b_in.shape[-1]])
                    emb_b = cls._create_tf_gpu_predict_embed(meta, b,
                                                             meta['hidden_layers_sizes_b'],
                                                             name='a_and_b' if meta['share_embedding'] else 'b')
                    # reshape back
                    intent_embed = tf.reshape(emb_b, [shape[0], shape[1], meta['embed_dim']])

                    sim_op, _ = cls._tf_gpu_sim(meta, word_embed, intent_embed)

                    all_intents_embed_in = tf.placeholder(tf.float32, (None, None, meta['embed_dim']),
                                                          name='all_intents_embed')
                    sim_all, _ = cls._tf_gpu_sim(meta, word_embed, all_intents_embed_in)

                    saver = tf.train.Saver()

                else:
                    saver = tf.train.import_meta_graph(checkpoint + '.meta')

                    a_in = tf.get_collection('message_placeholder')[0]
                    b_in = tf.get_collection('intent_placeholder')[0]

                    sim_op = tf.get_collection('similarity_op')[0]

                    all_intents_embed_in = tf.get_collection('all_intents_embed_in')[0]
                    sim_all = tf.get_collection('sim_all')[0]

                    word_embed = tf.get_collection('word_embed')[0]
                    intent_embed = tf.get_collection('intent_embed')[0]

                saver.restore(sess, checkpoint)

            with io.open(os.path.join(
                    model_dir,
                    cls.name + "_inv_intent_dict.pkl"), 'rb') as f:
                inv_intent_dict = pickle.load(f)
            with io.open(os.path.join(
                    model_dir,
                    cls.name + "_encoded_all_intents.pkl"), 'rb') as f:
                encoded_all_intents = pickle.load(f)
            with io.open(os.path.join(
                    model_dir,
                    cls.name + "_all_intents_embed_values.pkl"), 'rb') as f:
                all_intents_embed_values = pickle.load(f)

            return cls(
                component_config=meta,
                inv_intent_dict=inv_intent_dict,
                encoded_all_intents=encoded_all_intents,
                all_intents_embed_values=all_intents_embed_values,
                session=sess,
                graph=graph,
                message_placeholder=a_in,
                intent_placeholder=b_in,
                similarity_op=sim_op,
                all_intents_embed_in=all_intents_embed_in,
                sim_all=sim_all,
                word_embed=word_embed,
                intent_embed=intent_embed,
            )

        else:
            logger.warning("Failed to load nlu model. Maybe path {} "
                           "doesn't exist"
                           "".format(os.path.abspath(model_dir)))
            return cls(component_config=meta)


class ChronoBiasLayerNormBasicLSTMCell(tf.contrib.rnn.LayerNormBasicLSTMCell):
    """Custom LayerNormBasicLSTMCell that allows chrono initialization
        of gate biases.

        See super class for description.

        See https://arxiv.org/abs/1804.11188
        for details about chrono initialization
    """

    def __init__(self,
                 num_units,
                 forget_bias=1.0,
                 input_bias=0.0,
                 activation=tf.tanh,
                 layer_norm=True,
                 norm_gain=1.0,
                 norm_shift=0.0,
                 dropout_keep_prob=1.0,
                 dropout_prob_seed=None,
                 out_layer_size=None,
                 reuse=None):
        """Initializes the basic LSTM cell

        Additional args:
            input_bias: float, The bias added to input gates.
            out_layer_size: (optional) integer, The number of units in
                the optional additional output layer.
        """
        super(ChronoBiasLayerNormBasicLSTMCell, self).__init__(
            num_units,
            forget_bias=forget_bias,
            activation=activation,
            layer_norm=layer_norm,
            norm_gain=norm_gain,
            norm_shift=norm_shift,
            dropout_keep_prob=dropout_keep_prob,
            dropout_prob_seed=dropout_prob_seed,
            reuse=reuse
        )
        self._input_bias = input_bias
        self._out_layer_size = out_layer_size

    @property
    def output_size(self):
        return self._out_layer_size or self._num_units

    @property
    def state_size(self):
        return tf.contrib.rnn.LSTMStateTuple(self._num_units,
                                             self.output_size)

    @staticmethod
    def _dense_layer(args, layer_size):
        """Optional out projection layer"""
        proj_size = args.get_shape()[-1]
        dtype = args.dtype
        weights = tf.get_variable("kernel",
                                  [proj_size, layer_size],
                                  dtype=dtype)
        bias = tf.get_variable("bias",
                               [layer_size],
                               dtype=dtype)
        out = tf.nn.bias_add(tf.matmul(args, weights), bias)
        return out

    def call(self, inputs, state):
        """LSTM cell with layer normalization and recurrent dropout."""
        c, h = state
        args = tf.concat([inputs, h], 1)
        concat = self._linear(args)
        dtype = args.dtype

        i, j, f, o = tf.split(value=concat, num_or_size_splits=4, axis=1)
        if self._layer_norm:
            i = self._norm(i, "input", dtype=dtype)
            j = self._norm(j, "transform", dtype=dtype)
            f = self._norm(f, "forget", dtype=dtype)
            o = self._norm(o, "output", dtype=dtype)

        g = self._activation(j)
        if (not isinstance(self._keep_prob, float)) or self._keep_prob < 1:
            g = tf.nn.dropout(g, self._keep_prob, seed=self._seed)

        new_c = (c * tf.sigmoid(f + self._forget_bias) +
                 g * tf.sigmoid(i + self._input_bias))  # added input_bias

        # do not do layer normalization on the new c,
        # because there are no trainable weights
        # if self._layer_norm:
        #     new_c = self._norm(new_c, "state", dtype=dtype)

        new_h = self._activation(new_c) * tf.sigmoid(o)

        # added dropout to the hidden state h
        if (not isinstance(self._keep_prob, float)) or self._keep_prob < 1:
            new_h = tf.nn.dropout(new_h, self._keep_prob, seed=self._seed)

        # add postprocessing of the output
        if self._out_layer_size is not None:
            with tf.variable_scope('out_layer'):
                new_h = self._dense_layer(new_h, self._out_layer_size)

        new_state = tf.contrib.rnn.LSTMStateTuple(new_c, new_h)
        return new_h, new_state