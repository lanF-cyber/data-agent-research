export type Arm = "full" | "reminder" | "selected";
type Cell = string | number | null;
type TablePage = {offset: number; total: number; rows: Cell[][]};
export type Graph = {
  id: string; experiment: string; mode: string; ticket: string; questionId: string; case: string; arm: Arm; parent: string;
  elements: {id: string}[]; links: {fromElementId: string; toElementId: string; cue?: string}[];
};
export type Receipt = {
  state: string; ticket: string; arm: Arm; case: string; variant: string; error?: string;
  question: string; sql: string; rows: number; columns: string[]; first_rows: Cell[][];
  history_turns: number[]; set_match: boolean; receipt_checked: boolean; note: string; followup: string; table_page?: TablePage;
};

export async function dispatch(canvas: Graph, operation: string, view?: number): Promise<Receipt> {
  const response = await fetch("/rsi-skills/api/engine/flow", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      flowId: "@training.cosql/FLOW_HARNESS", params: {structure: JSON.stringify({...canvas, operation, view})},
    })});
  const envelope = await response.json();
  if (!response.ok || !envelope.result) throw new Error("RSI 调度失败，请核对来源、原文片段或服务状态。");
  const r: Receipt = JSON.parse(envelope.data.value.result);
  if (!r.receipt_checked || r.ticket !== canvas.ticket) throw new Error("平台回执校验失败。");
  if (r.state === "failed") throw new Error(r.error ?? "执行失败");
  return r;
}
