#
# Copyright (c) 2012-2018, CRS4
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import json
import os
import pickle
import shutil
import tempfile
import unittest

from hl7apy.exceptions import ManifestMismatch, ManifestPrivacyError, MissingSchemaDependency
from hl7apy.manifest import (
    build_manifest,
    copy_artifacts,
    dumps_manifest,
    find_private_values,
    infer_profile_version,
    load_entry_profile,
    main,
    parse_message_with_manifest,
    read_manifest,
    select_entry,
    verify_manifest,
    write_manifest,
)

_BASE_PATH = os.path.abspath(os.path.dirname(__file__))
_ITI_PROFILE = os.path.join(_BASE_PATH, 'profiles/iti_21')
_PDQ_PROFILE = os.path.join(_BASE_PATH, '../examples/iti_21/pdq_req')

_RSP_K21 = (
    'MSH|^~\\&|SENDING APP|SENDING FAC|RECEIVING APP|RECEIVING FAC|20140410170011||RSP^K22^RSP_K21|11111111|P|2.5\r'
    'MSA|AA|20140410170015\r'
    'QAK|222222222|OK\r'
    'QPD|IHE PDQ Query|222222222|@PID.3.1.1^3333333|90|\r'
    'PID|1||10101109091948^^^GATEWAY&1.3.6.1.4.1.21367.2011.2.5.17&ISO||JOHN^SMITH^^^^^A||19690113|M\r'
)


def _is_sequence(reference):
    return isinstance(reference, (tuple, list)) and len(reference) == 2 and reference[0] in ('sequence', 'choice')


def _without_profile_field(reference, field_name):
    if not _is_sequence(reference):
        return reference
    content, children = reference
    return (
        content,
        tuple((child[0], _without_profile_field(child[1], field_name)) + tuple(child[2:]) for child in children if child[0] != field_name),
    )


def _write_profile(directory, name, profile):
    path = os.path.join(directory, name)
    with open(path, 'wb') as profile_file:
        pickle.dump(profile, profile_file, protocol=2)
    return path


class TestSchemaManifest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        with open(_ITI_PROFILE, 'rb') as profile_file:
            self.iti_profile = pickle.load(profile_file)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_manifest_is_deterministic_and_schema_only(self):
        manifest = build_manifest([_ITI_PROFILE, _PDQ_PROFILE], validation={'validation_level': 'STRICT'}, name='batch')
        again = build_manifest([_ITI_PROFILE, _PDQ_PROFILE], validation={'validation_level': 'STRICT'}, name='batch')
        self.assertEqual(dumps_manifest(manifest), dumps_manifest(again))
        self.assertEqual(manifest['summary']['versions'], ['2.5'])
        self.assertEqual([], find_private_values(manifest))
        self.assertNotIn(self.temp_dir, dumps_manifest(manifest))
        for entry in manifest['entries']:
            self.assertTrue(entry['artifact'].startswith('profiles/'))
            self.assertEqual(entry['validation']['level'], 'STRICT')
            self.assertTrue(entry['validation']['force_validation'] is False)
            self.assertTrue(entry['validation']['find_groups'] is True)

    def test_manifest_rejects_message_values(self):
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'})
        manifest['entries'][0]['message'] = _RSP_K21
        with self.assertRaises(ManifestPrivacyError):
            write_manifest(manifest, os.path.join(self.temp_dir, 'manifest.json'))
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'})
        manifest['entries'][0]['sample'] = 'PID|1||123456^^^HOSP^MR||DOE^JOHN'
        with self.assertRaises(ManifestPrivacyError):
            dumps_manifest(manifest)

    def test_extensions_include_dependency_chain(self):
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'})
        extensions = {extension['path']: extension for extension in manifest['entries'][0]['extensions']}
        qpd_4 = extensions['RSP_K21/QPD/QPD_4']
        self.assertEqual(qpd_4['status'], 'extension')
        self.assertEqual(
            [item['name'] for item in qpd_4['dependency_chain']],
            ['iti_21', 'RSP_K21', 'QPD', 'QPD_4'],
        )
        self.assertIn('RSP_K21/QPD/QPD_3', extensions)
        self.assertEqual(extensions['RSP_K21/QPD/QPD_3']['status'], 'override')
        self.assertEqual(extensions['RSP_K21/QPD/QPD_3']['datatype'], 'QIP')

    def test_missing_extension_raises_chain_instead_of_standard_fallback(self):
        reduced = dict(self.iti_profile)
        reduced['RSP_K21'] = _without_profile_field(reduced['RSP_K21'], 'QPD_4')
        reduced_path = _write_profile(self.temp_dir, 'iti_21_missing_qpd_4', reduced)
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'}, name='batch')
        manifest['entries'][0]['profile']['sha256'] = ''
        manifest['artifacts'][manifest['entries'][0]['artifact']]['sha256'] = ''
        manifest['entries'][0]['dependencies'][3]['sha256'] = ''
        with self.assertRaises(MissingSchemaDependency) as raised:
            verify_manifest(manifest, profiles={'iti_21': reduced_path})
        self.assertEqual(
            [item['name'] for item in raised.exception.chain],
            ['iti_21', 'RSP_K21', 'QPD', 'QPD_4'],
        )

    def test_verify_rejects_tampered_profile(self):
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'})
        tampered = dict(self.iti_profile)
        tampered['RSP_K21'] = _without_profile_field(tampered['RSP_K21'], 'QPD_8')
        tampered_path = _write_profile(self.temp_dir, 'tampered', tampered)
        with self.assertRaises(ManifestMismatch):
            verify_manifest(manifest, profiles={'iti_21': tampered_path})
        verified = verify_manifest(manifest, profiles={'iti_21': _ITI_PROFILE})
        self.assertTrue(verified.ok)
        self.assertEqual(verified.entries[0]['extensions'], len(manifest['entries'][0]['extensions']))

    def test_multi_version_entries_are_independent(self):
        manifest = build_manifest(
            {
                'iti_25': {'path': _ITI_PROFILE, 'version': '2.5'},
                'iti_26': {'path': _ITI_PROFILE, 'version': '2.6'},
            },
            validation={'validation_level': 'STRICT'},
            name='multi',
        )
        self.assertEqual(manifest['summary']['versions'], ['2.5', '2.6'])
        self.assertEqual(len({entry['artifact'] for entry in manifest['entries']}), 1)
        self.assertEqual(
            {entry['hl7_version'] for entry in manifest['entries']},
            {'2.5', '2.6'},
        )
        for entry in manifest['entries']:
            dependency = [dep for dep in entry['dependencies'] if dep['kind'] == 'hl7-standard'][0]
            self.assertEqual(dependency['version'], entry['hl7_version'])
        report = verify_manifest(
            manifest,
            profiles={'iti_25': _ITI_PROFILE, 'iti_26': _ITI_PROFILE},
        )
        self.assertTrue(report.ok)
        entry = select_entry(manifest, 'RSP_K21', '2.6')
        self.assertEqual(entry['hl7_version'], '2.6')
        with self.assertRaises(MissingSchemaDependency):
            select_entry(manifest, 'RSP_K21', '2.7')

    def test_parse_uses_manifest_version_and_validation(self):
        manifest = build_manifest(
            {'iti_21': {'path': _ITI_PROFILE, 'version': '2.5'}},
            validation={'validation_level': 'STRICT', 'find_groups': True},
            name='batch',
        )
        manifest_path = write_manifest(manifest, os.path.join(self.temp_dir, 'manifest.json'))
        message = parse_message_with_manifest(
            _RSP_K21, manifest_path, profiles={'iti_21': _ITI_PROFILE}
        )
        self.assertEqual(message.qpd.qpd_3.datatype, 'QIP')
        self.assertTrue(message.validate())
        with self.assertRaises(MissingSchemaDependency):
            parse_message_with_manifest(
                _RSP_K21.replace('|2.5\r', '|2.6\r'),
                manifest_path,
                profiles={'iti_21': _ITI_PROFILE},
            )

    def test_parse_refuses_missing_profile(self):
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'}, name='batch')
        manifest_path = write_manifest(manifest, os.path.join(self.temp_dir, 'manifest.json'))
        with self.assertRaises(MissingSchemaDependency) as raised:
            parse_message_with_manifest(_RSP_K21, manifest_path)
        self.assertIn('profile artifact not found', str(raised.exception))

    def test_entry_profile_requires_exact_artifact(self):
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'})
        with self.assertRaises(ManifestMismatch):
            load_entry_profile(manifest, entry_id='iti_21', profiles={'iti_21': _PDQ_PROFILE})
        profile = load_entry_profile(manifest, entry_id='iti_21', profiles={'iti_21': _ITI_PROFILE})
        self.assertIn('RSP_K21', profile)

    def test_infer_profile_version_prefers_exact_standard(self):
        self.assertEqual(infer_profile_version('RSP_K21', self.iti_profile['RSP_K21']), '2.5')
        self.assertEqual(infer_profile_version('RSP_K21', self.iti_profile['RSP_K21'], ['2.6', '2.5']), '2.5')

    def test_read_manifest_enforces_schema_only(self):
        manifest = build_manifest([_ITI_PROFILE], validation={'validation_level': 'STRICT'})
        path = write_manifest(manifest, os.path.join(self.temp_dir, 'manifest.json'))
        with open(path) as manifest_file:
            data = json.load(manifest_file)
        data['entries'][0]['er7'] = _RSP_K21
        with open(path, 'w') as manifest_file:
            json.dump(data, manifest_file)
        with self.assertRaises(ManifestPrivacyError):
            read_manifest(path)

    def test_cli_build_and_verify(self):
        bundle = os.path.join(self.temp_dir, 'bundle')
        os.mkdir(bundle)
        manifest_path = os.path.join(bundle, 'cli.json')
        self.assertEqual(0, main(['build', _ITI_PROFILE, '-o', manifest_path, '--name', 'cli']))
        manifest = read_manifest(manifest_path)
        self.assertEqual(manifest['summary']['entries'], 1)
        self.assertEqual(1, main(['verify', manifest_path, '--json']))
        profiles = os.path.join(bundle, 'profiles')
        os.mkdir(profiles)
        shutil.copy(_ITI_PROFILE, os.path.join(profiles, 'iti_21'))
        self.assertEqual(0, main(['verify', manifest_path, '--json']))

    def test_copy_artifacts_builds_portable_bundle(self):
        manifest = build_manifest([_ITI_PROFILE, _PDQ_PROFILE], validation={'validation_level': 'STRICT'})
        bundle = os.path.join(self.temp_dir, 'bundle')
        os.mkdir(bundle)
        manifest_path = write_manifest(manifest, os.path.join(bundle, 'manifest.json'))
        copy_artifacts(
            manifest,
            {'profiles/iti_21': _ITI_PROFILE, 'profiles/pdq_req': _PDQ_PROFILE},
            bundle,
        )
        self.assertEqual(sorted(os.listdir(os.path.join(bundle, 'profiles'))), ['iti_21', 'pdq_req'])
        self.assertTrue(verify_manifest(manifest_path).ok)
        with self.assertRaises(MissingSchemaDependency):
            copy_artifacts(manifest, {}, os.path.join(self.temp_dir, 'incomplete'))

    def test_legacy_profile_is_rejected(self):
        legacy_path = os.path.join(_BASE_PATH, 'profiles/old_pharm_h4')
        with self.assertRaises(Exception):
            build_manifest([legacy_path])


if __name__ == '__main__':
    unittest.main()
