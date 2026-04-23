/** REST API client for dashboard actions */

interface ActionResult {
  status?: string;
  error?: string;
  workflow?: string;
  cwd?: string;
  stderr_log?: string;
}

export async function actionReview(logFile: string): Promise<ActionResult> {
  const resp = await fetch('/api/action/review', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ log_file: logFile }),
  });
  return resp.json() as Promise<ActionResult>;
}

export async function actionInvestigate(logFile: string): Promise<ActionResult> {
  const resp = await fetch('/api/action/investigate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ log_file: logFile }),
  });
  return resp.json() as Promise<ActionResult>;
}

export async function actionRestart(logFile: string): Promise<ActionResult> {
  const resp = await fetch('/api/action/restart', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ log_file: logFile }),
  });
  return resp.json() as Promise<ActionResult>;
}
