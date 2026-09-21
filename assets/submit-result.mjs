// Pure submission tool: no files, shell commands, network, or session relaunch.
export default async function () {
  const sdk = process.env.CODEX_WORKER_TOOL_SDK;
  if (!sdk) throw new Error('Result submission SDK is not configured');
  const { tool } = await import(sdk);
  return {
    tool: {
      codex_worker_submit_result: tool({
        description: 'Submit the final result after all development and validation. Invalid arguments return an error: correct only the report and call again in this same session. After acceptance, finish without other tool calls.',
        args: {
          status: tool.schema.enum(['completed', 'needs_escalation']),
          changed: tool.schema.array(tool.schema.string()),
          validation: tool.schema.array(tool.schema.string()),
          risk: tool.schema.string(),
          validation_commands: tool.schema.array(tool.schema.string()).optional(),
        },
        async execute(args) {
          if (!['completed', 'needs_escalation'].includes(args.status)) throw new Error('Invalid status');
          for (const field of ['changed', 'validation']) {
            if (!Array.isArray(args[field]) || args[field].some(x => typeof x !== 'string' || !x.trim())) {
              throw new Error(`${field} must contain nonempty strings; correct the report and resubmit`);
            }
          }
          if (!args.validation.length) throw new Error('validation requires actual results or a blocker');
          if (typeof args.risk !== 'string' || !args.risk.trim()) throw new Error('risk is required; use none if no known risk');
          const report = { status: args.status, changed: args.changed, validation: args.validation, risk: args.risk };
          if (args.validation_commands !== undefined) {
            const commands = args.validation_commands;
            if (!Array.isArray(commands) || commands.length > 10 || commands.some(x => typeof x !== 'string' || !x.trim() || Array.from(x).length > 256)) {
              throw new Error('validation_commands requires at most 10 nonempty commands of at most 256 characters');
            }
            if (new Set(commands).size !== commands.length) throw new Error('validation_commands must not contain duplicates');
            report.validation_commands = commands;
          }
          if (Array.from(JSON.stringify(report)).length > 1800) throw new Error('Report exceeds 1800 characters; shorten only the report and resubmit');
          return JSON.stringify({ accepted: true, report });
        },
      }),
    },
  };
}
