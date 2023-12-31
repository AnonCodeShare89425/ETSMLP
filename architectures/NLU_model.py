# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""
RoBERTa: A Robustly Optimized BERT Pretraining Approach.
"""

import logging

import torch
import torch.nn as nn
import torch.nn.functional as F
from fairseq import utils
from fairseq.models import (
    FairseqEncoder,
    FairseqEncoderModel,
    register_model,
    register_model_architecture,
)
from fairseq.modules import LayerNorm, TransformerSentenceEncoder
from fairseq.modules.quant_noise import quant_noise as apply_quant_noise_
from fairseq.modules.transformer_sentence_encoder import init_bert_params
from ..module.smlp_encoder import SMLPSentenceEncoder
from fairseq.models.roberta.hub_interface import RobertaHubInterface

logger = logging.getLogger(__name__)


@register_model("smlp_mlm")
class SMLP_MLM_Model(FairseqEncoderModel):


    def __init__(self, args, encoder):
        super().__init__(encoder)
        self.args = args

        # We follow BERT's random weight initialization
        # self.apply(init_bert_params)

        self.classification_heads = nn.ModuleDict()

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        parser.add_argument(
            "--encoder-layers", type=int, metavar="L", help="num encoder layers"
        )
        parser.add_argument(
            "--encoder-embed-dim",
            type=int,
            metavar="H",
            help="encoder embedding dimension",
        )
        parser.add_argument(
            "--encoder-ffn-embed-dim",
            type=int,
            metavar="F",
            help="encoder embedding dimension for FFN",
        )
        parser.add_argument(
            "--encoder-attention-heads",
            type=int,
            metavar="A",
            help="num encoder attention heads",
        )
        parser.add_argument(
            "--activation-fn",
            choices=utils.get_available_activation_fns(),
            help="activation function to use",
        )
        parser.add_argument(
            "--pooler-activation-fn",
            choices=utils.get_available_activation_fns(),
            help="activation function to use for pooler layer",
        )
        parser.add_argument(
            "--encoder-normalize-before",
            action="store_true",
            help="apply layernorm before each encoder block",
        )
        parser.add_argument(
            "--dropout", type=float, metavar="D", help="dropout probability"
        )
        parser.add_argument(
            "--attention-dropout",
            type=float,
            metavar="D",
            help="dropout probability for attention weights",
        )
        parser.add_argument(
            "--activation-dropout",
            type=float,
            metavar="D",
            help="dropout probability after activation in FFN",
        )
        parser.add_argument(
            "--pooler-dropout",
            type=float,
            metavar="D",
            help="dropout probability in the masked_lm pooler layers",
        )
        parser.add_argument(
            "--max-positions", type=int, help="number of positional embeddings to learn"
        )
        parser.add_argument(
            "--load-checkpoint-heads",
            action="store_true",
            help="(re-)register and load heads when loading checkpoints",
        )
        # args for "Reducing Transformer Depth on Demand with Structured Dropout" (Fan et al., 2019)
        parser.add_argument(
            "--encoder-layerdrop",
            type=float,
            metavar="D",
            default=0,
            help="LayerDrop probability for encoder",
        )
        parser.add_argument(
            "--encoder-layers-to-keep",
            default=None,
            help="which layers to *keep* when pruning as a comma-separated list",
        )
        # args for Training with Quantization Noise for Extreme Model Compression ({Fan*, Stock*} et al., 2020)
        parser.add_argument(
            "--quant-noise-pq",
            type=float,
            metavar="D",
            default=0,
            help="iterative PQ quantization noise at training time",
        )
        parser.add_argument(
            "--quant-noise-pq-block-size",
            type=int,
            metavar="D",
            default=8,
            help="block size of quantization noise at training time",
        )
        parser.add_argument(
            "--quant-noise-scalar",
            type=float,
            metavar="D",
            default=0,
            help="scalar quantization noise and scalar quantization at training time",
        )
        parser.add_argument(
            "--untie-weights-roberta",
            action="store_true",
            help="Untie weights between embeddings and classifiers in RoBERTa",
        )
        parser.add_argument(
            "--spectral-norm-classification-head",
            action="store_true",
            default=False,
            help="Apply spectral normalization on the classification head",
        )

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure all arguments are present
        base_architecture(args)

        if not hasattr(args, "max_positions"):
            args.max_positions = args.tokens_per_sample

        encoder = RobertaEncoder(args, task.source_dictionary)
        return cls(args, encoder)

    def forward(
        self,
        src_tokens,
        features_only=False,
        return_all_hiddens=False,
        classification_head_name=None,
        **kwargs
    ):
        if classification_head_name is not None:
            features_only = True

        x, extra = self.encoder(src_tokens, features_only, return_all_hiddens, **kwargs)

        if classification_head_name is not None:
            x = self.classification_heads[classification_head_name](x,**kwargs)
        return x, extra

    @classmethod
    def from_pretrained(
        cls,
        model_name_or_path,
        checkpoint_file="model.pt",
        data_name_or_path=".",
        bpe="gpt2",
        **kwargs
    ):
        from fairseq import hub_utils

        x = hub_utils.from_pretrained(
            model_name_or_path,
            checkpoint_file,
            data_name_or_path,
            archive_map=cls.hub_models(),
            bpe=bpe,
            load_checkpoint_heads=True,
            **kwargs,
        )
        cls.upgrade_args(x["args"])

        logger.info(x["args"])
        return RobertaHubInterface(x["args"], x["task"], x["models"][0])

    def get_normalized_probs(self, net_output, log_probs, sample=None):
        """Get normalized probabilities (or log probs) from a net's output."""
        logits = net_output[0].float()
        if log_probs:
            return F.log_softmax(logits, dim=-1)
        else:
            return F.softmax(logits, dim=-1)

    def register_classification_head(
        self, name, num_classes=None, inner_dim=None, **kwargs
    ):
        """Register a classification head."""
        if name in self.classification_heads:
            prev_num_classes = self.classification_heads[name].out_proj.out_features
            prev_inner_dim = self.classification_heads[name].dense.out_features
            if num_classes != prev_num_classes or inner_dim != prev_inner_dim:
                logger.warning(
                    're-registering head "{}" with num_classes {} (prev: {}) '
                    "and inner_dim {} (prev: {})".format(
                        name, num_classes, prev_num_classes, inner_dim, prev_inner_dim
                    )
                )
        self.classification_heads[name] = SMLPClassificationHead(
            input_dim=self.args.encoder_embed_dim,
            inner_dim=inner_dim or self.args.encoder_embed_dim,
            num_classes=num_classes,
            activation_fn=self.args.pooler_activation_fn,
            pooler_dropout=self.args.pooler_dropout,
            q_noise=self.args.quant_noise_pq,
            qn_block_size=self.args.quant_noise_pq_block_size,
            do_spectral_norm=self.args.spectral_norm_classification_head,
            sen_rep_type=self.args.sen_rep_type
        )

    @property
    def supported_targets(self):
        return {"self"}


    def upgrade_state_dict_named(self, state_dict, name):
        prefix = name + "." if name != "" else ""

        # rename decoder -> encoder before upgrading children modules
        for k in list(state_dict.keys()):
            if k.startswith(prefix + "decoder"):
                new_k = prefix + "encoder" + k[len(prefix + "decoder") :]
                state_dict[new_k] = state_dict[k]
                del state_dict[k]

        # upgrade children modules
        super().upgrade_state_dict_named(state_dict, name)

        # Handle new classification heads present in the state dict.
        current_head_names = (
            []
            if not hasattr(self, "classification_heads")
            else self.classification_heads.keys()
        )
        keys_to_delete = []
        for k in state_dict.keys():
            if not k.startswith(prefix + "classification_heads."):
                continue

            head_name = k[len(prefix + "classification_heads.") :].split(".")[0]
            num_classes = state_dict[
                prefix + "classification_heads." + head_name + ".out_proj.weight"
            ].size(0)
            inner_dim = state_dict[
                prefix + "classification_heads." + head_name + ".dense.weight"
            ].size(0)

            if getattr(self.args, "load_checkpoint_heads", False):
                if head_name not in current_head_names:
                    self.register_classification_head(head_name, num_classes, inner_dim)
            else:
                if head_name not in current_head_names:
                    logger.warning(
                        "deleting classification head ({}) from checkpoint "
                        "not present in current model: {}".format(head_name, k)
                    )
                    keys_to_delete.append(k)
                elif (
                    num_classes
                    != self.classification_heads[head_name].out_proj.out_features
                    or inner_dim
                    != self.classification_heads[head_name].dense.out_features
                ):
                    logger.warning(
                        "deleting classification head ({}) from checkpoint "
                        "with different dimensions than current model: {}".format(
                            head_name, k
                        )
                    )
                    keys_to_delete.append(k)
        for k in keys_to_delete:
            del state_dict[k]

        # Copy any newly-added classification heads into the state dict
        # with their current weights.
        if hasattr(self, "classification_heads"):
            cur_state = self.classification_heads.state_dict()
            for k, v in cur_state.items():
                if prefix + "classification_heads." + k not in state_dict:
                    logger.info("Overwriting " + prefix + "classification_heads." + k)
                    state_dict[prefix + "classification_heads." + k] = v


class SMLPLMHead(nn.Module):
    """Head for masked language modeling."""

    def __init__(self, embed_dim, output_dim, activation_fn, weight=None):
        super().__init__()
        self.dense = nn.Linear(embed_dim, embed_dim)
        self.activation_fn = utils.get_activation_fn(activation_fn)
        self.layer_norm = LayerNorm(embed_dim)

        if weight is None:
            weight = nn.Linear(embed_dim, output_dim, bias=False).weight
        self.weight = weight
        self.bias = nn.Parameter(torch.zeros(output_dim))

    def forward(self, features, masked_tokens=None, **kwargs):
        # Only project the masked tokens while training,
        # saves both memory and computation
        if masked_tokens is not None:
            features = features[masked_tokens, :]

        x = self.dense(features)
        x = self.activation_fn(x)
        x = self.layer_norm(x)
        # project back to size of vocabulary with bias
        x = F.linear(x, self.weight) + self.bias
        return x


class SMLPClassificationHead(nn.Module):
    """Head for sentence-level classification tasks."""

    def __init__(
        self,
        input_dim,
        inner_dim,
        num_classes,
        activation_fn,
        pooler_dropout,
        q_noise=0,
        qn_block_size=8,
        do_spectral_norm=False,
        sen_rep_type = 'cls'
    ):
        super().__init__()
        self.dense = nn.Linear(input_dim, inner_dim)
        self.activation_fn = utils.get_activation_fn(activation_fn)
        self.dropout = nn.Dropout(p=pooler_dropout)
        self.out_proj = apply_quant_noise_(
            nn.Linear(inner_dim, num_classes), q_noise, qn_block_size
        )
        logger.info("Using sentence representation type : %s"%sen_rep_type)
        self.sen_rep_type=sen_rep_type
        if do_spectral_norm:
            if q_noise != 0:
                raise NotImplementedError(
                    "Attempting to use Spectral Normalization with Quant Noise. This is not officially supported"
                )
            self.out_proj = torch.nn.utils.spectral_norm(self.out_proj)

    def forward(self, features, **kwargs):

        if self.sen_rep_type=='mp':
            if "src_lengths" in kwargs.keys():
                src_lengths=kwargs["src_lengths"]
                x = features.sum(dim=1) / src_lengths.unsqueeze(-1)
            else:
                x =torch.mean(features, dim=1)
        elif self.sen_rep_type=='cls':
            x = features[:, 0, :]  # take <s> token (equiv. to [CLS])
        else:
            raise NotImplementedError("You have to decide the sentence embeding methods.")
        x = self.dropout(x)
        x = self.dense(x)
        x = self.activation_fn(x)
        x = self.dropout(x)
        x = self.out_proj(x)
        return x


class RobertaEncoder(FairseqEncoder):
    """RoBERTa encoder."""

    def __init__(self, args, dictionary):
        super().__init__(dictionary)
        self.args = args

        if args.encoder_layers_to_keep:
            args.encoder_layers = len(args.encoder_layers_to_keep.split(","))

        self.sentence_encoder = SMLPSentenceEncoder(args,padding_idx=dictionary.pad(),
                vocab_size=len(dictionary),
                num_encoder_layers=args.encoder_layers,
                embedding_dim=args.encoder_embed_dim,
                embedding_type='sparse',
                dropout=args.dropout,
                max_seq_len=args.max_positions,
                use_position_embeddings=args.use_position_embeddings,
                offset_positions_by_padding=True,
                encoder_normalize_before=getattr(args, "encoder_normalize_before", False),
                learned_pos_embedding=args.encoder_learned_pos,
                sen_rep_type=getattr(args, 'sen_rep_type', 'cls'),
                freeze=getattr(args, 'freeze', False))

        args.untie_weights_roberta = getattr(args, "untie_weights_roberta", False)

        self.lm_head = SMLPLMHead(
            embed_dim=args.encoder_embed_dim,
            output_dim=len(dictionary),
            activation_fn=args.activation_fn,
            weight=(
                self.sentence_encoder.embed_tokens.embed.weight
                if not args.untie_weights_roberta
                else None
            ),
        )

    def forward(
        self,
        src_tokens,
        features_only=False,
        return_all_hiddens=False,
        masked_tokens=None,
        **unused
    ):
        """
        Args:
            src_tokens (LongTensor): input tokens of shape `(batch, src_len)`
            features_only (bool, optional): skip LM head and just return
                features. If True, the output will be of shape
                `(batch, src_len, embed_dim)`.
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).

        Returns:
            tuple:
                - the LM output of shape `(batch, src_len, vocab)`
                - a dictionary of additional data, where 'inner_states'
                  is a list of hidden states. Note that the hidden
                  states have shape `(src_len, batch, vocab)`.
        """
        if "src_lengths" in unused.keys():
            src_lengths = unused['src_lengths']
        else:
            src_lengths = None
        x, extra = self.extract_features(
            src_tokens, src_lengths=src_lengths, return_all_hiddens=return_all_hiddens
        )
        if not features_only:
            x = self.output_layer(x, masked_tokens=masked_tokens)
        return x, extra

    def extract_features(self, src_tokens, src_lengths=None, return_all_hiddens=False, **kwargs):
        inner_states,_ = self.sentence_encoder(src_tokens, src_lengths, last_state_only=not return_all_hiddens)
        # inner_states, _ = self.sentence_encoder(
        #     src_tokens,
        #     last_state_only=not return_all_hiddens
        # )
        features = inner_states[0].transpose(0, 1)  # T x B x C -> B x T x C
        return features, {"inner_states": inner_states if return_all_hiddens else None}

    def output_layer(self, features, masked_tokens=None, **unused):
        return self.lm_head(features, masked_tokens)

    def max_positions(self):
        """Maximum output length supported by the encoder."""
        return self.args.max_positions


@register_model_architecture("smlp_mlm", "smlp_mlm")
def base_architecture(args):
    args.encoder_layers = getattr(args, "encoder_layers", 12)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 768)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 12)

    args.activation_fn = getattr(args, "activation_fn", "gelu")
    args.pooler_activation_fn = getattr(args, "pooler_activation_fn", "tanh")

    args.dropout = getattr(args, "dropout", 0.1)
    args.pooler_dropout = getattr(args, "pooler_dropout", 0.0)
    args.encoder_layers_to_keep = getattr(args, "encoder_layers_to_keep", None)
    args.encoder_layerdrop = getattr(args, "encoder_layerdrop", 0.0)

    args.spectral_norm_classification_head = getattr(
        args, "spectral_nrom_classification_head", False
    )
    args.share_encoder_input_output_embed = getattr(args, 'share_encoder_input_output_embed', True)
    args.encoder_learned_pos = getattr(args, 'encoder_learned_pos', False)
    args.no_token_positional_embeddings = getattr(args, 'no_token_positional_embeddings', False)
    args.sent_loss = getattr(args, 'sent_loss', True)

    args.normalize_embedding = getattr(args, 'normalize_embedding', False)
    args.adaptive_input = getattr(args, "adaptive_input", False)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.encoder_q_dim = getattr(args, "encoder_q_dim", 768)
    args.encoder_k_dim = getattr(args, "encoder_k_dim", 768)
    args.attention_dropout = getattr(args, "attention_dropout", 0.0)
    args.activation_dropout = getattr(args, "activation_dropout", 0.0)
    args.activation_fn = getattr(args, "activation_fn", "relu")
    args.use_position_embeddings = getattr(args, "use_position_embeddings", True)
    args.smlp_pos = getattr(args, 'smlp_pos', 'before_act')
    args.has_ffn = getattr(args, 'has_ffn', False)
    args.kernal_cutoff = getattr(args, 'kernal_cutoff', False)
    args.complex = getattr(args, 'complex', False)
    args.complex_version = getattr(args, 'complex_version', 'normal')
    args.no_beta = getattr(args, 'no_beta', False)
    args.norm_type = getattr(args,'norm_type','layernorm')
    args.max_lambda = getattr(args, "max_lambda", 0.9999)
    args.norm_after_smlp = getattr(args, "norm_after_smlp", False)
    args.cls_attn = getattr(args, "cls_attn", False)
    args.gate = getattr(args, 'gate', False)
    args.freeze = getattr(args,"freeze",False)


@register_model_architecture("smlp_mlm", "smlp_mlm_complex")
def smlp_mlm_complex_architecture(args):
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", True)
    args.use_position_embeddings = getattr(args, "use_position_embeddings", False)
    args.complex = getattr(args, 'complex', True)
    args.r_max=getattr(args, 'r_max', 0.9)
    args.r_min=getattr(args, 'r_min', 0.1)
    args.max_phase=getattr(args, 'max_phase', 6.28)
    args.dt_min=getattr(args, 'dt_min', 1e-3)
    args.dt_max = getattr(args, 'dt_max', 0.1)
    # args.cls_attn = getattr(args, "cls_attn", True)
    args.gate_activation_fn = getattr(args,'gate_activation_fn','sigmoid')
    base_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_mp")
def smlp_mlm_complex_architecture_mp(args):
    # args.freeze = getattr(args,"freeze",False)
    args.sen_rep_type = getattr(args, 'sen_rep_type', 'mp')
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_QQP")
def smlp_mlm_complex_architecture_test1(args):
    args.r_max=getattr(args, 'r_max', 0.9)
    args.r_min=getattr(args, 'r_min', 0.1)
    args.max_phase=getattr(args, 'max_phase', 6.28)
    args.sen_rep_type = getattr(args, 'sen_rep_type', 'mp')
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_k_dim = getattr(args, "encoder_k_dim",512)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_sst2")
def smlp_mlm_complex_architecture_sst2(args):
    args.r_max=getattr(args, 'r_max', 0.9)
    args.r_min=getattr(args, 'r_min', 0.1)
    args.max_phase=getattr(args, 'max_phase', 6.28)
    args.sen_rep_type = getattr(args, 'sen_rep_type', 'mp')
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_k_dim = getattr(args, "encoder_k_dim",512)
    args.encoder_layers = getattr(args, "encoder_layers", 12)
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_sst2_gate")
def smlp_mlm_complex_architecture_qqp_gate(args):
    args.gate = getattr(args, 'gate', True)
    smlp_mlm_complex_architecture_sst2(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_QQP_gate")
def smlp_mlm_complex_architecture_sst2_gate(args):
    args.gate = getattr(args, 'gate', True)
    smlp_mlm_complex_architecture_sst2(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_cola")
def smlp_mlm_complex_architecture_cola(args):
    args.r_max=getattr(args, 'r_max', 0.9)
    args.r_min=getattr(args, 'r_min', 0.1)
    args.max_phase=getattr(args, 'max_phase', 6.28)
    args.sen_rep_type = getattr(args, 'sen_rep_type', 'mp')
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_k_dim = getattr(args, "encoder_k_dim",512)
    args.encoder_layers = getattr(args, "encoder_layers", 3)
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_cola_gate")
def smlp_mlm_complex_architecture_cola_gate(args):
    args.gate = getattr(args, 'gate', True)
    smlp_mlm_complex_architecture_cola(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_mrpc")
def smlp_mlm_complex_architecture_mrpc(args):
    args.r_max=getattr(args, 'r_max', 0.9)
    args.r_min=getattr(args, 'r_min', 0.1)
    args.max_phase=getattr(args, 'max_phase', 6.28)
    args.sen_rep_type = getattr(args, 'sen_rep_type', 'mp')
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_k_dim = getattr(args, "encoder_k_dim",512)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_mrpc_gate")
def smlp_mlm_complex_architecture_mrpc_gate(args):
    args.gate = getattr(args, 'gate', True)
    smlp_mlm_complex_architecture_mrpc(args)


@register_model_architecture("smlp_mlm", "smlp_mlm_complex_mnli")
def smlp_mlm_complex_architecture_test1_base(args):
    args.encoder_layers = getattr(args, "encoder_layers", 12)
    smlp_mlm_complex_architecture_test1(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_mnli_gate")
def smlp_mlm_complex_architecture_test1_base(args):
    args.gate = getattr(args, 'gate', True)
    args.encoder_layers = getattr(args, "encoder_layers", 12)
    smlp_mlm_complex_architecture_test1(args)

@register_model_architecture('smlp_mlm', 'smlp_mlm_complex_gate')
def smlp_mlm_complex_gate(args):
    args.decoder_layers = getattr(args, "decoder_layers", 16)
    args.gate = getattr(args, 'gate', True)
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_qnli")
def smlp_mlm_complex_architecture_qnli(args):
    args.r_max=getattr(args, 'r_max', 0.9)
    args.r_min=getattr(args, 'r_min', 0.1)
    args.max_phase=getattr(args, 'max_phase', 6.28)
    args.sen_rep_type = getattr(args, 'sen_rep_type', 'mp')
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_k_dim = getattr(args, "encoder_k_dim",512)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_qnli_gate")
def smlp_mlm_complex_architecture_qnli_gate(args):
    args.gate = getattr(args, 'gate', True)
    # args.encoder_layers = getattr(args, "encoder_layers", 12)
    smlp_mlm_complex_architecture_test1(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_imdb")
def smlp_mlm_complex_architecture_imdb(args):
    args.r_max=getattr(args, 'r_max', 0.9)
    args.r_min=getattr(args, 'r_min', 0.1)
    args.max_phase=getattr(args, 'max_phase', 3.14)
    args.sen_rep_type = getattr(args, 'sen_rep_type', 'mp')
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_k_dim = getattr(args, "encoder_k_dim",512)
    args.encoder_layers = getattr(args, "encoder_layers", 4)
    smlp_mlm_complex_architecture(args)

@register_model_architecture("smlp_mlm", "smlp_mlm_complex_imdb_gate")
def smlp_mlm_complex_architecture_imdb_gate(args):
    args.gate = getattr(args, 'gate', True)
    smlp_mlm_complex_architecture_imdb(args)