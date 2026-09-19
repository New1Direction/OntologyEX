"""Release-facing checks. No model calls or native-host success claims."""
from html.parser import HTMLParser
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/New1Direction/OntologyEX'


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links, self.ids, self.scripts, self.handlers = [], [], [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'a' and 'href' in values:
            self.links.append(values['href'])
        if 'id' in values:
            self.ids.append(values['id'])
        if tag == 'script':
            self.scripts.append(values)
        self.handlers.extend(key for key in values if key.startswith('on'))


class ReleaseSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / 'index.html').read_text(encoding='utf-8')
        self.page = Page(self.text)

    def test_public_links_reach_current_workflow(self):
        for link in (REPO, 'docs/payments-demo.html', REPO + '/blob/main/docs/native-pilot.md',
                     REPO + '/actions/workflows/validator.yml'):
            self.assertIn(link, self.page.links)
        self.assertNotIn('unzip ontology-extraction.skill', self.text)
        self.assertIn('python examples/payments-domain/demo.py', self.text)
        self.assertIn('ontology-extraction/scripts/install.py', self.text)

    def test_local_links_and_fragments_resolve(self):
        self.assertEqual(len(self.page.ids), len(set(self.page.ids)))
        for href in self.page.links:
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc:
                self.assertEqual(parsed.scheme, 'https')
                continue
            if parsed.path:
                target = (ROOT / parsed.path).resolve()
                self.assertTrue(target.is_relative_to(ROOT.resolve()))
                self.assertTrue(target.is_file(), href)
            elif parsed.fragment:
                self.assertIn(parsed.fragment, self.page.ids)

    def test_static_page_has_no_active_scripts(self):
        self.assertEqual(self.page.scripts, [])
        self.assertEqual(self.page.handlers, [])
        self.assertIn('Content-Security-Policy', self.text)
        self.assertIn('Skip to content', self.text)

    def test_frontmatter_sources_are_not_transformed_by_pages(self):
        self.assertTrue((ROOT / '.nojekyll').is_file())
        for name in ('SKILL.md', 'METHOD.md'):
            self.assertTrue((ROOT / 'ontology-extraction' / name).is_file())

    def test_evaluation_claims_remain_explicit(self):
        self.assertIn('NOT_RUN', self.text)
        self.assertIn('pre-authored', self.text)
        self.assertIn('Checks are not permissions.', self.text)
        protocol = (ROOT / 'docs/native-pilot.md').read_text(encoding='utf-8')
        self.assertIn('**Status: NOT_RUN.**', protocol)
        self.assertIn('pilot.py prepare --out', protocol)
        self.assertIn('grade_adapter.py', protocol)
        self.assertIn('--acknowledge-code-execution', protocol)

    def test_source_only_native_pilot_installation(self):
        # Run the documented source preparation and installer from a separate directory.
        # No recipe, adapter, compiler submission, or LLM host is executed.
        code = r'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'examples/cachetools-domain'))
import case
import domain_skill as ds
files, pin = case.source_files()
ds.write_files(Path(sys.argv[2]), files)
print(ds.canonical({'paths': sorted(files), 'upstream_commit': pin['commit']}).decode())
'''
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / 'fresh project'
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT), str(project)],
                                    cwd=temp, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            expected = ['LICENSE', 'TASK.md', 'src/cachetools/__init__.py', 'src/cachetools/keys.py']
            self.assertEqual(data['paths'], expected)
            self.assertEqual(sorted(p.relative_to(project).as_posix() for p in project.rglob('*')
                                    if p.is_file()), expected)
            install = subprocess.run([sys.executable, '-B', str(ROOT / 'ontology-extraction/scripts/install.py'),
                                      '--project', str(project)], cwd=temp, env=env,
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(install.returncode, 0, install.stderr + install.stdout)
            receipt = json.loads(install.stdout)
            self.assertEqual(receipt['status'], 'INSTALLED_PROJECT_FILES')
            self.assertEqual(receipt['host_activation'], 'NOT_RUN')
            self.assertFalse((project / 'adapter.py').exists())
            self.assertFalse((project / 'workspaces').exists())
            self.assertFalse((project / 'build').exists())
            installed = project / '.claude/skills/ontology-extraction'
            self.assertTrue((installed / 'SKILL.md').is_file())
            help_result = subprocess.run([sys.executable, '-B', str(installed / 'scripts/onboard.py'), '--help'],
                                         cwd=temp, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)


if __name__ == '__main__':
    unittest.main()
