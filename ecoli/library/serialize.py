import re
from unum import Unum
from bson.codec_options import TypeEncoder
from vivarium.core.registry import Serializer
from vivarium.library.topology import convert_path_style, normalize_path

from ecoli.library.parameters import Parameter, param_store


class UnumSerializer(Serializer):
    
    def __init__(self):
        super().__init__()
        self.regex_for_serialized = re.compile('!UnumSerializer\\[(.*)\\]')

    python_type = Unum
    def serialize(self, value):
        num = str(value.asNumber())
        assert ' ' not in num
        return f'!UnitsSerializer[{num} {value.strUnit()}]'
        
    def can_deserialize(self, data):
        if not isinstance(data, str):
            return False
        return bool(self.regex_for_serialized.fullmatch(data))

    def deserialize(self, data):
        # WARNING: This deserialization is lossy and drops the unit
        # information since there is no easy way to parse Unum's unit
        # strings.
        matched_regex = self.regex_for_serialized.fullmatch(data)
        if matched_regex:
            data = matched_regex.group(1)
        return float(data.split(' ')[0])


class ParameterSerializer(Serializer):
    
    def __init__(self):
        super().__init__()
        self.regex_for_serialized = re.compile('!ParameterSerializer\\[(.*)\\]')
    
    python_type = Parameter
        
    def can_deserialize(self, data):
        if not isinstance(data, str):
            return False
        return bool(self.regex_for_serialized.fullmatch(data))

    def deserialize(self, data):
        matched_regex = self.regex_for_serialized.fullmatch(data)
        if matched_regex:
            data = matched_regex.group(1)
        path = normalize_path(convert_path_style(data))
        return param_store.get(path)
