import copy
from abc import ABC, abstractmethod
from typing import Dict

import torch
from torch import nn

from TTS.tts.utils.data import sequence_mask
from TTS.utils.generic_utils import format_aux_input
from TTS.utils.training import gradual_training_scheduler


class TacotronAbstract(ABC, nn.Module):
    def __init__(
        self,
        num_chars,
        num_speakers,
        r,
        postnet_output_dim=80,
        decoder_output_dim=80,
        attn_type="original",
        attn_win=False,
        attn_norm="softmax",
        prenet_type="original",
        prenet_dropout=True,
        prenet_dropout_at_inference=False,
        forward_attn=False,
        trans_agent=False,
        forward_attn_mask=False,
        location_attn=True,
        attn_K=5,
        separate_stopnet=True,
        bidirectional_decoder=False,
        double_decoder_consistency=False,
        ddc_r=None,
        encoder_in_features=512,
        decoder_in_features=512,
        d_vector_dim=None,
        use_gst=False,
        gst=None,
        gradual_training=None,
    ):
        """Abstract Tacotron class"""
        super().__init__()
        self.num_chars = num_chars
        self.r = r
        self.decoder_output_dim = decoder_output_dim
        self.postnet_output_dim = postnet_output_dim
        self.use_gst = use_gst
        self.gst = gst
        self.num_speakers = num_speakers
        self.bidirectional_decoder = bidirectional_decoder
        self.double_decoder_consistency = double_decoder_consistency
        self.ddc_r = ddc_r
        self.attn_type = attn_type
        self.attn_win = attn_win
        self.attn_norm = attn_norm
        self.prenet_type = prenet_type
        self.prenet_dropout = prenet_dropout
        self.prenet_dropout_at_inference = prenet_dropout_at_inference
        self.forward_attn = forward_attn
        self.trans_agent = trans_agent
        self.forward_attn_mask = forward_attn_mask
        self.location_attn = location_attn
        self.attn_K = attn_K
        self.separate_stopnet = separate_stopnet
        self.encoder_in_features = encoder_in_features
        self.decoder_in_features = decoder_in_features
        self.d_vector_dim = d_vector_dim
        self.gradual_training = gradual_training

        # layers
        self.embedding = None
        self.encoder = None
        self.decoder = None
        self.postnet = None

        # multispeaker
        if self.d_vector_dim is None:
            # if d_vector_dim is None we need use the nn.Embedding, with default d_vector_dim
            self.use_d_vectors = False
        else:
            # if d_vector_dim is not None we need use speaker embedding per sample
            self.use_d_vectors = True

        # global style token
        if self.gst and use_gst:
            self.decoder_in_features += self.gst.gst_embedding_dim  # add gst embedding dim
            self.gst_layer = None

        # model states
        self.embedded_speakers = None
        self.embedded_speakers_projected = None

        # additional layers
        self.decoder_backward = None
        self.coarse_decoder = None

    @staticmethod
    def _format_aux_input(aux_input: Dict) -> Dict:
        return format_aux_input({"d_vectors": None, "speaker_ids": None}, aux_input)

    #############################
    # INIT FUNCTIONS
    #############################

    def _init_states(self):
        self.embedded_speakers = None
        self.embedded_speakers_projected = None

    def _init_backward_decoder(self):
        self.decoder_backward = copy.deepcopy(self.decoder)

    def _init_coarse_decoder(self):
        self.coarse_decoder = copy.deepcopy(self.decoder)
        self.coarse_decoder.r_init = self.ddc_r
        self.coarse_decoder.set_r(self.ddc_r)

    #############################
    # CORE FUNCTIONS
    #############################

    @abstractmethod
    def forward(self):
        pass

    @abstractmethod
    def inference(self):
        pass

    def load_checkpoint(
        self, config, checkpoint_path, eval=False
    ):  # pylint: disable=unused-argument, redefined-builtin
        state = torch.load(checkpoint_path, map_location=torch.device("cpu"))
        self.load_state_dict(state["model"])
        self.decoder.set_r(state["r"])
        if eval:
            self.eval()
            assert not self.training

    #############################
    # COMMON COMPUTE FUNCTIONS
    #############################

    def compute_masks(self, text_lengths, mel_lengths):
        """Compute masks  against sequence paddings."""
        # B x T_in_max (boolean)
        input_mask = sequence_mask(text_lengths)
        output_mask = None
        if mel_lengths is not None:
            max_len = mel_lengths.max()
            r = self.decoder.r
            max_len = max_len + (r - (max_len % r)) if max_len % r > 0 else max_len
            output_mask = sequence_mask(mel_lengths, max_len=max_len)
        return input_mask, output_mask

    def _backward_pass(self, mel_specs, encoder_outputs, mask):
        """Run backwards decoder"""
        decoder_outputs_b, alignments_b, _ = self.decoder_backward(
            encoder_outputs, torch.flip(mel_specs, dims=(1,)), mask
        )
        decoder_outputs_b = decoder_outputs_b.transpose(1, 2).contiguous()
        return decoder_outputs_b, alignments_b

    def _coarse_decoder_pass(self, mel_specs, encoder_outputs, alignments, input_mask):
        """Double Decoder Consistency"""
        T = mel_specs.shape[1]
        if T % self.coarse_decoder.r > 0:
            padding_size = self.coarse_decoder.r - (T % self.coarse_decoder.r)
            mel_specs = torch.nn.functional.pad(mel_specs, (0, 0, 0, padding_size, 0, 0))
        decoder_outputs_backward, alignments_backward, _ = self.coarse_decoder(
            encoder_outputs.detach(), mel_specs, input_mask
        )
        # scale_factor = self.decoder.r_init / self.decoder.r
        alignments_backward = torch.nn.functional.interpolate(
            alignments_backward.transpose(1, 2), size=alignments.shape[1], mode="nearest"
        ).transpose(1, 2)
        decoder_outputs_backward = decoder_outputs_backward.transpose(1, 2)
        decoder_outputs_backward = decoder_outputs_backward[:, :T, :]
        return decoder_outputs_backward, alignments_backward

    #############################
    # EMBEDDING FUNCTIONS
    #############################

    def compute_speaker_embedding(self, speaker_ids):
        """Compute speaker embedding vectors"""
        if hasattr(self, "speaker_embedding") and speaker_ids is None:
            raise RuntimeError(" [!] Model has speaker embedding layer but speaker_id is not provided")
        if hasattr(self, "speaker_embedding") and speaker_ids is not None:
            self.embedded_speakers = self.speaker_embedding(speaker_ids).unsqueeze(1)
        if hasattr(self, "speaker_project_mel") and speaker_ids is not None:
            self.embedded_speakers_projected = self.speaker_project_mel(self.embedded_speakers).squeeze(1)

    def compute_gst(self, inputs, style_input, speaker_embedding=None):
        """Compute global style token"""
        if isinstance(style_input, dict):
            query = torch.zeros(1, 1, self.gst.gst_embedding_dim // 2).type_as(inputs)
            if speaker_embedding is not None:
                query = torch.cat([query, speaker_embedding.reshape(1, 1, -1)], dim=-1)

            _GST = torch.tanh(self.gst_layer.style_token_layer.style_tokens)
            gst_outputs = torch.zeros(1, 1, self.gst.gst_embedding_dim).type_as(inputs)
            for k_token, v_amplifier in style_input.items():
                key = _GST[int(k_token)].unsqueeze(0).expand(1, -1, -1)
                gst_outputs_att = self.gst_layer.style_token_layer.attention(query, key)
                gst_outputs = gst_outputs + gst_outputs_att * v_amplifier
        elif style_input is None:
            gst_outputs = torch.zeros(1, 1, self.gst.gst_embedding_dim).type_as(inputs)
        else:
            gst_outputs = self.gst_layer(style_input, speaker_embedding)  # pylint: disable=not-callable
        inputs = self._concat_speaker_embedding(inputs, gst_outputs)
        return inputs

    @staticmethod
    def _add_speaker_embedding(outputs, embedded_speakers):
        embedded_speakers_ = embedded_speakers.expand(outputs.size(0), outputs.size(1), -1)
        outputs = outputs + embedded_speakers_
        return outputs

    @staticmethod
    def _concat_speaker_embedding(outputs, embedded_speakers):
        embedded_speakers_ = embedded_speakers.expand(outputs.size(0), outputs.size(1), -1)
        outputs = torch.cat([outputs, embedded_speakers_], dim=-1)
        return outputs

    #############################
    # CALLBACKS
    #############################

    def on_epoch_start(self, trainer):
        """Callback for setting values wrt gradual training schedule.

        Args:
            trainer (TrainerTTS): TTS trainer object that is used to train this model.
        """
        if self.gradual_training:
            r, trainer.config.batch_size = gradual_training_scheduler(trainer.total_steps_done, trainer.config)
            trainer.config.r = r
            self.decoder.set_r(r)
            if trainer.config.bidirectional_decoder:
                trainer.model.decoder_backward.set_r(r)
            trainer.train_loader = trainer.setup_train_dataloader(self.ap, self.model.decoder.r, verbose=True)
            trainer.eval_loader = trainer.setup_eval_dataloder(self.ap, self.model.decoder.r)
            print(f"\n > Number of output frames: {self.decoder.r}")
