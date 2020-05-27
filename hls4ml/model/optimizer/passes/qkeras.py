from ..optimizer import OptimizerPass
from ....model.hls_layers import BatchNormalization
from ....model.hls_model import IntegerPrecisionType, FixedPrecisionType, register_layer
from ....templates import templates
import tensorflow as tf
import numpy as np
from qkeras import get_quantizer

class OutputRoundingSaturationMode(OptimizerPass):
    '''
    Set the Rounding and Saturation mode of the output (and accumulator, if applicable)
    of the layers specific in layer list.
    The layer list is empty by default.
    To specify which layer to apply this pass to, perform e.g.:
    hls4ml.model.optimizer.OutputRoundingSaturationMode.layers = ['Dense', 'Activation', 'BatchNormalization']
    The Rounding and Saturation modes are 'None' by default (so use the compiler defaults)
    To set which mode to use:
    hls4ml.model.optimizer.OutputRoundingSaturationMode.rounding_mode = 'AP_RND_CONV'
    hls4ml.model.optimizer.OutputRoundingSaturationMode.saturation_mode = 'AP_SAT'
    '''

    layers = [] 
    rounding_mode = None 
    saturation_mode = None 
    saturation_bits = None

    def match(self, node):
        layer_match = node.__class__.__name__ in self.layers or node.name in self.layers
        t = str(node.get_output_variable().type.precision)
        # check that the type doesn't already contain the rounding mode
        rs_match = False
        if self.rounding_mode is not None:
            rs_match = rs_match or not (self.rounding_mode in t)
        if self.saturation_mode is not None:
            rs_match = rs_match or not (self.saturation_mode in t)
        return layer_match and rs_match

    def transform(self, model, node):
        oldtype = node.get_output_variable().type.precision
        if isinstance(oldtype, IntegerPrecisionType):
            newprecision = IntegerPrecisionType(oldtype.width, oldtype.signed, self.rounding_mode, self.saturation_mode, self.saturation_bits)
        elif isinstance(oldtype, FixedPrecisionType):
            newtype = FixedPrecisionType(oldtype.width, oldtype.integer, oldtype.signed, self.rounding_mode, self.saturation_mode, self.saturation_bits)
        else: # in case the precision is a string
            newtype = self.precision_string_modify(oldtype)
        node.get_output_variable().type.precision = newtype
        if node.get_attr('accum_t') is not None:
            node.set_attr('accum_t', newtype)
        return False

    def precision_string_modify(self, pstr):
        # For when the type is a string not an Type
        mode = ''
        if self.rounding_mode is not None:
            mode += ',' + self.rounding_mode
        if self.saturation_mode is not None:
            mode += ',' + self.saturation_mode
        if self.saturation_bits is not None:
            mode += ',' + str(self.saturation_bits)
        mode += '>'
        pstr = pstr.replace('>', mode)
        return pstr

class ApplyAlpha(BatchNormalization):
    ''' A custom layer to scale the output of a QDense layer which used 'alpha != 1'
        Inference computation uses BatchNormalization methods'''

    def initialize(self):
        inp = self.get_input_variable()
        shape = inp.shape
        dims = inp.dim_names
        self.add_output_variable(shape, dims)

    def add_weights(self, scale, bias):
        self.add_weights_variable(name='scale', var_name='s{index}', data=scale)
        self.add_weights_variable(name='bias', var_name='b{index}', data=bias)

# register the layer and its templates
register_layer('ApplyAlpha', ApplyAlpha)
# TODO ideally: for backend in backends
temps = templates.get_backend('Vivado')
temps.register_templates('ApplyAlpha', temps.get_function_template('BatchNormalization'), temps.get_config_template('BatchNormalization'))

class QKerasFactorizeAlpha(OptimizerPass):
    '''OptimizerPass for extracting alpha "scale" from QKeras quantized layer.
       The weights of the Q{Dense, Conv} layer are scaled to the common data type,
       and an 'ApplyAlpha' layer is inserted to reapply the scale.
    '''
    def match(self, node):
        q_layer = node.__class__.__name__ in ["Dense", "Conv1D", "Conv2D"]
        has_w_quant = node.get_attr('weight_quantizer') is not None 
        has_b_quant = node.get_attr('bias_quantizer') is not None
        has_w_alpha, has_b_alpha = False, False
        if has_w_quant:
            if hasattr(node.get_attr('weight_quantizer'), 'alpha'):
                has_w_alpha = node.get_attr('weight_quantizer').alpha != 1
            else:
                has_w_alpha = False
        if has_b_quant:
            if hasattr(node.get_attr('bias_quantizer'), 'alpha'):
                has_b_alpha = node.get_attr('bias_quantizer').alpha != 1
            else:
                has_b_alpha = False
        is_match = q_layer and ((has_w_quant and has_w_alpha) or (has_b_quant and has_b_alpha))
        return is_match

    def transform(self, model, node):
        # The quantizer has to be applied to set the scale attribute
        # This must be applied to the _unquantized_ weights to obtain the correct scale
        quantizer = node.weights['weight'].quantizer.quantizer_fn # get QKeras quantizer
        weights = node.weights['weight'].data_unquantized # get weights
        qweights = quantizer(tf.convert_to_tensor(weights))
        scale = quantizer.scale.numpy()
        unscale = 1. / scale

        new_weights = unscale * qweights # use the quantized weights for safety


        # Set the alpha to 1 to avoid hitting this pass again
        qcfg = quantizer.get_config()
        qcfg['alpha'] = 1
        node.weights['weight'].quantizer.quantizer_fn = quantizer.from_config(qcfg)

        # update the weights also applying the hls4ml quantizer
        # this is only needed for the binary layers which encode -1 as 0
        node.weights['weight'].data = node.weights['weight'].quantizer(new_weights.numpy())

        has_w_quant = node.get_attr('weight_quantizer') is not None 
        has_b_quant = node.get_attr('bias_quantizer') is not None
        if has_w_quant: 
            node.attributes['weight_quantizer'].alpha = 1
        if has_b_quant:
            node.attributes['bias_quantizer'].alpha = 1

        # insert a Batch Normalization layer to apply the alpha scale
        attrs = {
            'name' : node.get_attr('name') + '_alpha',
            'class_name' : 'Alpha',
            'inputs' : node.outputs,
            'n_in' : node.get_attr('n_out'),
            'n_filt' : node.get_attr('n_filt') if node.get_attr('n_filt') is not None else -1,
            'reuse_factor' : node.get_attr('reuse_factor'),
            'bias_t' : 'ap_fixed<16,6>', # TODO automate this
            'scale_t' : 'ap_fixed<16,6>' # TODO automate this
        }
        alpha_layer = model.make_node('ApplyAlpha', node.name + '_alpha', attrs, node.outputs)
        alpha_layer.add_weights(scale, np.zeros(scale.shape))
        model.insert_node(alpha_layer)
        return True

class FuseConsecutiveBatchNormalization(OptimizerPass):
    '''OptimizerPass to merge consecutive BatchNormalization layers.
       These may exist in a model after QKerasFactorizeAlpha layer.
       Scale and Bias of each layer are combined into scale and bias of a single layer.
    '''

    def match(self, node):
        return isinstance(node, BatchNormalization) and \
               isinstance(node.get_input_node(), BatchNormalization)

    def transform(self, model, node):
        bn0 = node.get_input_node()
        bn1 = node

        s0 = bn0.weights['scale'].data
        b0 = bn0.weights['bias'].data
        s1 = bn1.weights['scale'].data
        b1 = bn1.weights['bias'].data

        s2 = s0 * s1
        b2 = s1 * b0 + b1

        bn0.weights['scale'].data = s2
        bn0.weights['bias'].data = b2

        model.remove_node(node, rewire=True)
        return True
