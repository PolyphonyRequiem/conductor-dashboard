/** REST API client for dashboard actions */

interface ActionResult {
  status?: string;
  error?: string;
  workflow?: string;
  cwd?: string;
  stderr_log?: string;
  pid?: number;
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

export async function actionStop(logFile: string): Promise<ActionResult> {
  const resp = await fetch('/api/action/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ log_file: logFile }),
  });
  return resp.json() as Promise<ActionResult>;
}

export async function openFile(filePath: string): Promise<void> {
  await fetch(`/api/open-file?path=${encodeURIComponent(filePath)}`);
}
