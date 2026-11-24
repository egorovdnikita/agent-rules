import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

SOURCE = Path(__file__).resolve().parents[1]

class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        shutil.copytree(SOURCE, self.root, ignore=shutil.ignore_patterns('.git', '.skill-build', '__pycache__'))
        self.home = Path(self.temp.name) / 'home'
    def tearDown(self):
        self.temp.cleanup()
    def run_script(self, command, check=True):
        return subprocess.run(['python3', str(self.root/'bin/skills.py'), command, '--home', str(self.home)], capture_output=True, check=check)
    def test_install_preserves_handoff_and_is_repeatable(self):
        handoff = self.home/'.agents/skills/handoff/SKILL.md'
        handoff.parent.mkdir(parents=True); handoff.write_text('preserve')
        self.run_script('install'); self.run_script('install')
        self.assertEqual(handoff.read_text(), 'preserve')
        for name in ['concepts','audit','frontend','import','typography']:
            a=self.home/'.agents/skills'/name/'SKILL.md'
            b=self.home/'.claude/skills'/name/'SKILL.md'
            self.assertEqual(a.resolve(), b.resolve())
            self.assertEqual(a.resolve(), (self.root/'skills'/name/'SKILL.md').resolve())
    def test_conflict_does_not_partially_install(self):
        conflict=self.home/'.claude/skills/typography';conflict.mkdir(parents=True)
        (conflict/'SKILL.md').write_text('keep')
        self.assertNotEqual(self.run_script('install',False).returncode,0)
        self.assertFalse((self.home/'.agents/skills/concepts').exists())
        self.assertEqual((conflict/'SKILL.md').read_text(),'keep')
    def test_reproducible_archives(self):
        self.run_script('build')
        manifest=(self.root/'.skill-build/manifest.json').read_bytes()
        self.run_script('build')
        self.assertEqual(manifest,(self.root/'.skill-build/manifest.json').read_bytes())
        for platform in ('claude-chat','chatgpt'):
            for archive in (self.root/'.skill-build'/platform).glob('*.zip'):
                with zipfile.ZipFile(archive) as z:
                    self.assertIsNone(z.testzip())
                    self.assertIn(archive.stem+'/SKILL.md',z.namelist())
    def test_mcp_update_and_conflict(self):
        spec=importlib.util.spec_from_file_location('server',self.root/'bin/skills_mcp.py')
        server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
        self.run_script('install')
        old=server.snapshot('audit')
        changed=old['content']+'\nТестовое изменение.\n'
        server.call('update_skill',dict(name='audit',expected_revision=old['revision'],content=changed))
        self.assertEqual((self.home/'.claude/skills/audit/SKILL.md').read_text(),changed)
        with self.assertRaises(ValueError):
            server.call('update_skill',dict(name='audit',expected_revision=old['revision'],content=old['content']))
        with self.assertRaises(ValueError):server.snapshot('../../CLAUDE')

if __name__=='__main__':unittest.main()
