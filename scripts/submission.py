"""Validated tool-result transport; final assistant prose is never a protocol."""
import hashlib
import json
from pathlib import Path

TOOL = 'codex_worker_submit_result'
LIMIT = 1800


def validate(report):
    if not isinstance(report, dict):
        raise ValueError('report must be an object')
    if report.get('status') not in ('completed', 'needs_escalation'):
        raise ValueError('status must be completed or needs_escalation')
    for field in ('changed', 'validation'):
        value = report.get(field)
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ValueError(field + ' must contain nonempty strings')
        if field == 'validation' and not value:
            raise ValueError('validation must contain at least one actual result or blocker')
    if not isinstance(report.get('risk'), str) or not report['risk'].strip():
        raise ValueError('risk must be a nonempty string')
    result = {key: report[key] for key in ('status', 'changed', 'validation', 'risk')}
    if len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) > LIMIT:
        raise ValueError('report exceeds 1800 characters; shorten the report, not the development work')
    return result


def configure(env):
    """Add one local plugin for this process only; use the existing OpenCode SDK."""
    roots = [Path(env['OPENCODE_CONFIG_DIR']).expanduser()] if env.get('OPENCODE_CONFIG_DIR') else []
    home = Path(env.get('HOME', str(Path.home())))
    roots += [Path(env.get('XDG_CONFIG_HOME', str(home / '.config'))) / 'opencode',
              Path(env.get('XDG_CACHE_HOME', str(home / '.cache'))) / 'opencode']
    sdk = next((root / 'node_modules/@opencode-ai/plugin/dist/tool.js' for root in roots
                if (root / 'node_modules/@opencode-ai/plugin/dist/tool.js').is_file()), None)
    if sdk is None:
        raise ValueError('OpenCode plugin SDK unavailable; no package was installed')
    config = json.loads(env.get('OPENCODE_CONFIG_CONTENT', '{}'))
    plugins = config.setdefault('plugin', [])
    if not isinstance(plugins, list):
        raise ValueError('OpenCode plugin configuration must be an array')
    plugin = (Path(__file__).resolve().parent.parent / 'assets/submit-result.mjs').as_uri()
    if plugin not in plugins:
        plugins.append(plugin)
    # This new capability only returns validated data, including in read-only mode.
    config['agent']['codex-worker']['permission'][TOOL] = 'allow'
    env['CODEX_WORKER_TOOL_SDK'] = sdk.resolve().as_uri()
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)


class Capture:
    def __init__(self):
        self.report = None
        self.error = 'missing_submission'
        self.canonical = None
        self.conflict = False
        self.seen = {}

    def consume(self, part, session):
        name = part.get('tool')
        state = part.get('state', {})
        if name != TOOL:
            if self.report is not None:
                self.report = None
                self.canonical = None
                self.error = 'activity_after_submission; submit again after final validation'
            return
        status = state.get('status')
        if status not in ('completed', 'error'):
            self.report = None
            self.error = 'submission_not_completed'
            return
        identity = part.get('callID') or part.get('id')
        if not identity or not session:
            self.report = None
            self.error = 'submission_identity_missing'
            return
        fingerprint = hashlib.sha256(json.dumps([status, state.get('output'), state.get('error')],
                                                sort_keys=True).encode()).hexdigest()
        if identity in self.seen:
            if self.seen[identity] != fingerprint:
                self.conflict = True
            else:
                return
        elif len(self.seen) >= 128:
            self.conflict = True
        else:
            self.seen[identity] = fingerprint
        self.report = None
        if self.conflict:
            self.error = 'conflicting_submissions'
            return
        if status == 'error':
            self.error = 'submission_rejected'
            return
        try:
            output = state.get('output')
            if not isinstance(output, str) or len(output) > 12000:
                raise ValueError('invalid submission envelope')
            envelope = json.loads(output)
            if not isinstance(envelope, dict) or envelope.get('accepted') is not True:
                raise ValueError('submission not accepted')
            report = validate(envelope.get('report'))
            canonical = json.dumps(report, ensure_ascii=False, sort_keys=True)
            if self.canonical is not None and self.canonical != canonical:
                self.conflict = True
                raise ValueError('conflicting_submissions')
            self.canonical = canonical
            self.report = report
            self.error = None
        except (ValueError, TypeError) as error:
            self.error = str(error)

    def public(self):
        return {'report': self.report, 'error': self.error}
