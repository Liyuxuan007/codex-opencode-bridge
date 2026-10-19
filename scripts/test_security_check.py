"""Synthetic fixtures exercise redaction checks without embedding real credentials."""
import unittest
from security_check import inspect


class SecurityTests(unittest.TestCase):
    def test_synthetic_credentials_are_rejected(self):
        for value in ['gh' + 'p_' + 'A' * 36, 's' + 'k-' + 'x' * 32,
                      '-----BEGIN ' + 'PRIVATE KEY-----',
                      'password=' + repr('a' * 24)]:
            self.assertTrue(inspect('README.md', value.encode()))

    def test_private_paths_and_unapproved_files_are_rejected(self):
        path = '/' + 'home' + '/example-person/project'
        self.assertTrue(inspect('README.md', path.encode()))
        self.assertTrue(inspect('config.json', b'{}'))
        self.assertTrue(inspect('README.md', b'\0'))
        self.assertTrue(inspect('README.md', b'x' * (1024 * 1024 + 1)))

    def test_template_is_safe(self):
        self.assertEqual(inspect('config.example.json', b'{"allowed_roots":["~/projects"]}'), [])


if __name__ == '__main__':
    unittest.main()
