import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import worker as w
class SharedSkillTests(unittest.TestCase):
 def test_generic_skill_discovery_catalog_only(self):
  with tempfile.TemporaryDirectory() as t:
   home=Path(t);root=home/'project';root.mkdir()
   for name in ('.agents/skills/manage-unity-workflows','.codex/skills/unity-cli','.codex/skills/other'):
    p=home/name;p.mkdir(parents=True);(p/'SKILL.md').write_text('original')
   (root/'.agents/skills/project-skill').mkdir(parents=True);(root/'.agents/skills/project-skill/SKILL.md').write_text('project')
   (root/'AGENTS.md').write_text('rules')
   with patch.object(w.Path,'home',return_value=home):
    ctx=w.discover_context(root)
   names={s['name'] for s in ctx['shared_skills']}
   self.assertEqual(names,{'manage-unity-workflows','unity-cli','other'})
   self.assertEqual([s['name'] for s in ctx['project_skills']],['project-skill'])
   self.assertEqual([i['name'] for i in ctx['instructions']],['AGENTS.md'])
   self.assertEqual(len(ctx['shared_roots']),2)
   # Catalog returns paths, never copied content.
   self.assertNotIn('original',json.dumps(ctx))
   prompt=w.context_prompt(ctx)
   self.assertIn('AGENTS.md',prompt);self.assertIn('project-skill',prompt)
 def test_manifest_capabilities_and_originals(self):
  with tempfile.TemporaryDirectory() as t:
   home=Path(t);root=home/'project';root.mkdir()
   p=home/'.agents/skills/manage-unity-workflows';p.mkdir(parents=True);(p/'SKILL.md').write_text('x')
   (root/'.opencode-worker.json').write_text(json.dumps({'capabilities':['unity'],'shared_skills':['manage-unity-workflows']}))
   with patch.object(w.Path,'home',return_value=home):ctx=w.discover_context(root)
   self.assertEqual(ctx['capabilities'],['unity'])
   self.assertEqual(ctx['shared_roots'],[str((home/'.agents/skills').resolve())])
   self.assertNotIn('original',json.dumps(ctx))
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'.opencode-worker.json').write_text('not json')
   ctx=w.discover_context(root,home=root)
   self.assertIn('invalid_manifest',ctx['manifest']['error'])
 def test_writer_and_read_only_are_distinct(self):
   roots=['/shared/unity-cli']
   writer=json.loads(w.worker_env(shared_roots=roots)['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
   self.assertEqual(writer['bash']['*'],'allow');self.assertEqual(writer['external_directory']['/shared/unity-cli/*'],'allow');self.assertEqual(writer['edit']['/shared/unity-cli/*'],'deny')
   read_only=json.loads(w.worker_env(read_only=True,shared_roots=roots)['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
   for name in ('edit','bash','task','write','patch'):self.assertEqual(read_only[name],'deny')
   self.assertEqual(read_only['*'],'deny')
 def test_symlinked_shared_skill_outside_catalogs_is_read_only(self):
   with tempfile.TemporaryDirectory() as t:
    home=Path(t)/'home';root=Path(t)/'project';home.mkdir();root.mkdir()
    base=home/'.agents/skills';base.mkdir(parents=True)
    # A real original skill lives outside the shared catalog; the catalog exposes
    # it only via a symlink whose resolved path is elsewhere.
    original=Path(t)/'external-origin/real-skill';original.mkdir(parents=True)
    (original/'SKILL.md').write_text('original')
    (original/'.env').write_text('SECRET=1')
    (base/'real-skill').symlink_to(original,target_is_directory=True)
    with patch.object(w.Path,'home',return_value=home):ctx=w.discover_context(root)
    resolved=str(original.resolve())
    self.assertIn(resolved,ctx['shared_roots'])
    perms=json.loads(w.worker_env(shared_roots=ctx['shared_roots'])['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
    self.assertEqual(perms['read'][resolved+'/*'],'allow')
    self.assertEqual(perms['external_directory'][resolved+'/*'],'allow')
    self.assertEqual(perms['edit'][resolved+'/*'],'deny')
    self.assertEqual(perms['edit'][resolved],'deny')
    # Credential denies stay last-match even after shared-root allows.
    keys=list(perms['read'])
    env_key=next(k for k in keys if k=='*.env')
    self.assertGreater(keys.index(env_key),keys.index(resolved+'/*'))
    self.assertEqual(perms['read']['*.env'],'deny')
