# Python SCALE Codec Library
#
# Copyright 2018-2020 Stichting Polkascan (Polkascan Foundation).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#  test_runtime_configuration.py
#

import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from scalecodec.base import RuntimeConfiguration, RuntimeConfigurationObject, ScaleBytes
from scalecodec.type_registry import load_type_registry_preset
from scalecodec.types import GenericVote, MultiAccountId


class TestScaleDecoderClasses(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.runtime_config = RuntimeConfigurationObject()
        cls.runtime_config.clear_type_registry()
        cls.runtime_config.update_type_registry(load_type_registry_preset("core"))
        cls.runtime_config.update_type_registry(load_type_registry_preset("legacy"))

    def test_valid_decoding_classes(self):
        for type_string in self.runtime_config.type_registry['types'].keys():

            decoding_cls = self.runtime_config.get_decoder_class(type_string)

            self.assertIsNotNone(decoding_cls, msg='"{}" didn\'t return decoding class'.format(type_string))


class TestMultipleRuntimeConfigurations(unittest.TestCase):

    def test_use_config_singleton(self):
        RuntimeConfiguration(config_id='test').update_type_registry({
            'types': {
                'CustomTestType': 'u8'
            }
        })
        self.assertIsNone(RuntimeConfiguration().get_decoder_class('CustomTestType'))
        self.assertIsNotNone(RuntimeConfiguration(config_id='test').get_decoder_class('CustomTestType'))

    def test_multiple_instances(self):
        runtime_config1 = RuntimeConfigurationObject()
        runtime_config1.update_type_registry({
            'types': {
                'MyNewType': 'Vec<u8>'
            }
        })

        runtime_config2 = RuntimeConfigurationObject()

        self.assertIsNone(RuntimeConfigurationObject().get_decoder_class('MyNewType'))
        self.assertIsNotNone(runtime_config1.get_decoder_class('MyNewType'))
        self.assertIsNone(runtime_config2.get_decoder_class('MyNewType'))

    def test_bytes_decoder_keeps_its_runtime_after_another_lookup(self):
        first = RuntimeConfigurationObject()
        second = RuntimeConfigurationObject()
        decoder = first.create_scale_object('Bytes', ScaleBytes('0x0401'))

        second.get_decoder_class('Bytes')
        second.type_registry = {'types': {}, 'runtime_api': {}}

        self.assertEqual('\x01', decoder.decode())
        self.assertIs(decoder.runtime_config, first)

    def test_registry_reload_is_isolated_across_threads(self):
        first = RuntimeConfigurationObject()
        second = RuntimeConfigurationObject()
        first.update_type_registry_types({'LocalAmount': 'u16'})
        second.update_type_registry_types({'LocalAmount': 'u8'})
        type_mapping = [('amount', 'LocalAmount')]
        decoder = first.create_scale_object('Struct', ScaleBytes('0x2a00'), type_mapping=type_mapping)
        encoder = first.create_scale_object('Struct', type_mapping=type_mapping)
        registry_cleared = Event()
        codec_used = Event()

        def reload_and_encode():
            second.get_decoder_class('Struct')
            second.clear_type_registry()
            registry_cleared.set()
            self.assertTrue(codec_used.wait(timeout=5))
            second.update_type_registry_types({'LocalAmount': 'u32'})
            return second.create_scale_object('Struct', type_mapping=type_mapping).encode({'amount': 42}).to_hex()

        with ThreadPoolExecutor(max_workers=1) as executor:
            reloaded = executor.submit(reload_and_encode)
            try:
                self.assertTrue(registry_cleared.wait(timeout=5))
                self.assertEqual({'amount': 42}, decoder.decode())
                self.assertEqual('0x2a00', encoder.encode({'amount': 42}).to_hex())
            finally:
                codec_used.set()

            self.assertEqual('0x2a000000', reloaded.result(timeout=5))

    def test_retained_decoder_class_supports_construction_and_decomposition(self):
        first = RuntimeConfigurationObject()
        second = RuntimeConfigurationObject()
        first.update_type_registry_types({'VoteAlias': 'GenericVote', 'Conviction': 'u16'})
        second.update_type_registry_types({'Conviction': 'u32'})
        vote_class = first.get_decoder_class('GenericVote')

        second.get_decoder_class('GenericVote')

        vote = vote_class()
        self.assertIsInstance(vote, GenericVote)
        self.assertIs(vote.runtime_config, first)
        self.assertIs(vote_class(runtime_config=second).runtime_config, second)
        self.assertEqual({'aye': 'bool', 'conviction': 'u16'}, vote_class.generate_type_decomposition())
        self.assertIs(first.create_scale_object('GenericVote', runtime_config=second).runtime_config, second)
        self.assertIs(vote_class, first.get_decoder_class('VoteAlias'))
        self.assertTrue(issubclass(vote_class, first.get_decoder_class('u8')))

    def test_codec_instance_can_be_encoded_in_another_runtime(self):
        first = RuntimeConfigurationObject()
        second = RuntimeConfigurationObject()
        value = first.create_scale_object('u8')
        value.encode(42)

        self.assertEqual('0x2a', second.create_scale_object('u8').encode(value).to_hex())

    def test_copied_registry_rebinds_decoder_classes(self):
        first = RuntimeConfigurationObject()
        first.update_type_registry_types({
            'ByteAlias': 'Bytes',
            'Payload': {'type': 'struct', 'type_mapping': [('value', 'ByteAlias')]},
        })
        decoder = first.create_scale_object('Payload', ScaleBytes('0x0441'))

        copy = deepcopy(decoder)
        copied_config = copy.runtime_config

        self.assertIsNot(copied_config, first)
        self.assertIs(copied_config.get_decoder_class('ByteAlias'), copied_config.get_decoder_class('Bytes'))
        self.assertTrue(issubclass(copied_config.get_decoder_class('Payload'), copied_config.get_decoder_class('Struct')))
        self.assertEqual({'value': 'A'}, copy.decode())

    def test_generated_classes_stay_out_of_default_registry_discovery(self):
        first = RuntimeConfigurationObject()
        type_name = 'RuntimeLocalStruct'
        first.update_type_registry_types({
            type_name: {'type': 'struct', 'type_mapping': [('amount', 'u16')]}
        })
        struct_class = first.get_decoder_class(type_name)

        second = RuntimeConfigurationObject()
        self.assertIsNone(second.get_decoder_class(type_name))

        first.clear_type_registry()
        self.assertIsNone(first.get_decoder_class(type_name))
        self.assertEqual({'amount': 42}, struct_class(data=ScaleBytes('0x2a00')).decode())

    def test_portable_primitive_metadata_is_local_to_each_definition(self):
        first = RuntimeConfigurationObject()
        second = RuntimeConfigurationObject()

        def register_primitive(runtime_config, type_id, doc):
            definition = runtime_config.create_scale_object('GenericRegistryType')
            definition.value = {'path': [], 'params': [], 'def': {'primitive': 'u8'}, 'docs': [doc]}
            runtime_config.update_from_scale_info_types([{'id': SimpleNamespace(value=type_id), 'type': definition}])
            return definition

        first_definition = register_primitive(first, 7, 'First runtime')
        other_definition = register_primitive(first, 8, 'Another definition in the first runtime')
        second_definition = register_primitive(second, 7, 'Second runtime')

        self.assertIs(first.get_decoder_class('scale_info::7').scale_info_type, first_definition)
        self.assertIs(first.get_decoder_class('scale_info::8').scale_info_type, other_definition)
        self.assertIs(second.get_decoder_class('scale_info::7').scale_info_type, second_definition)
        self.assertIsNone(first.get_decoder_class('u8').scale_info_type)
        value = first.create_scale_object('scale_info::7', ScaleBytes('0x2a'))
        self.assertEqual(42, value.decode())
        self.assertEqual('u8', value.generate_type_decomposition())

        byte_vector = first.create_scale_object('Vec<scale_info::7>', ScaleBytes('0x084142'))
        self.assertEqual('AB', byte_vector.decode())
        self.assertEqual('0x084142', byte_vector.encode('AB').to_hex())

        first.clear_type_registry()
        self.assertIsNone(first.get_decoder_class('scale_info::7'))
        self.assertIs(second.get_decoder_class('scale_info::7').scale_info_type, second_definition)

    def test_multisig_class_uses_its_bound_runtime_or_the_default(self):
        accounts = ['0x' + '22' * 32, '0x' + '11' * 32]
        first = RuntimeConfigurationObject()
        second = RuntimeConfigurationObject()
        first.update_type_registry_types({'AccountId': 'GenericAccountId'})
        multisig_class = first.get_decoder_class('MultiAccountId')
        second.get_decoder_class('MultiAccountId')

        bound_account = multisig_class.create_from_account_list(accounts, 2)
        self.assertIs(bound_account.runtime_config, first)

        with patch.dict(type(RuntimeConfiguration)._instances, clear=True):
            default = RuntimeConfiguration()
            direct_account = MultiAccountId.create_from_account_list(accounts, 2)
            self.assertIs(direct_account.runtime_config, default)

        self.assertEqual(bound_account.value, direct_account.value)
        self.assertEqual(sorted(accounts), direct_account.signatories)
        self.assertEqual(2, direct_account.threshold)


class TestRuntimeIdCache(unittest.TestCase):

    def test_runtime_id_cache_lookup(self):
        runtime_config = RuntimeConfigurationObject()
        runtime_config.update_type_registry(load_type_registry_preset("legacy"))
        runtime_config.update_type_registry(load_type_registry_preset("kusama"))

        self.assertEqual(1023, runtime_config.get_runtime_id_from_upgrades(54248))
        self.assertEqual(1020, runtime_config.get_runtime_id_from_upgrades(0))

    def test_set_head(self):
        runtime_config = RuntimeConfigurationObject()
        runtime_config.update_type_registry(load_type_registry_preset("legacy"))
        runtime_config.update_type_registry(load_type_registry_preset("kusama"))

        self.assertIsNone(runtime_config.get_runtime_id_from_upgrades(99999999998))

        # Set head to block
        runtime_config.set_runtime_upgrades_head(99999999999)

        # Check updated cache
        self.assertGreater(runtime_config.get_runtime_id_from_upgrades(99999999998), 0)


if __name__ == '__main__':
    unittest.main()
