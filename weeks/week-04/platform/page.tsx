"use client";

import React, { useEffect, useRef, useState } from "react";
import VisualDiagram from "@horsecoder/visual-core";
import type { VisualDiagramHandle } from "@horsecoder/visual-core/types";
import workflow from "./workflow.json";
import "./harness.css";

type Mode = "replay" | "live";
type Job = { ticket: string; mode: Mode; operation: "start" | "status" };
type Point = { x: number; y: number };
type Step = {
  tool: string;
  args: { sql?: string; table?: string; column?: string; term?: string };
  receipt: {
    accepted?: boolean;
    verified?: boolean;
    values?: string[];
    sample?: unknown[][];
    row_count?: number;
  };
};
type Receipt = {
  state: "running" | "complete" | "failed";
  ticket: string;
  mode: Mode;
  error?: string;
  checks_used: number;
  rows: number;
  memory_writes: number;
  set_match: boolean;
  actions: Step[];
};
const endpoint = "/dataTesting-core/api/engine/flow";
const operations: Record<string, string> = {
  catalog: "schema / profile",
  generate: "check_context",
  lookup_value: "lookup_value",
  submit: "submit_answer",
  learn: "verified memory",
};
const details: Record<string, string> = {
  catalog: "结构、外键、实际存值",
  generate: "读取回执，每问最多试查 3 次",
  lookup_value: "原库核实后返回生成节点",
  submit: "执行最终只读 SQL",
  learn: "交付成功后写入已核实值",
};

function NodeCard({
  element,
}: {
  element: { id: string; state: { name: string } };
}) {
  return (
    <div className={"h-node h-" + element.id}>
      <code>{operations[element.id]}</code>
      <strong>{element.state.name}</strong>
      <small>{details[element.id]}</small>
    </div>
  );
}

function Edge({
  link,
}: {
  link: {
    state: {
      id: string;
      points: Point[];
      toElementId: string;
      fromElementId: string;
    };
  };
}) {
  const p = link.state.points;
  if (!p?.length) return null;
  const source = p[0],
    target = p[p.length - 1];
  const branch =
    link.state.toElementId === "lookup_value" ||
    link.state.toElementId === "learn";
  const x1 = branch
    ? source.x + (link.state.fromElementId === "generate" ? 70 : 96)
    : source.x;
  const y1 = branch ? source.y - 38 : source.y;
  const x2 = branch ? target.x - 94 : target.x;
  const y2 = branch ? target.y + 38 : target.y;
  const mid = (y1 + y2) / 2;
  const line = branch
    ? "M " + x1 + " " + y1 + " H " + (x1 + x2) / 2 + " V " + y2 + " H " + x2
    : "M " + x1 + " " + y1 + " V " + mid + " H " + x2 + " V " + y2;
  const id = "edge-" + link.state.id,
    marker = "url(#" + id + ")";
  return (
    <g>
      <defs>
        <marker
          id={id}
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="5"
          markerHeight="5"
          orient="auto-start-reverse"
        >
          <path fill="#447a70" d="M0 0 8 4 0 8Z" />
        </marker>
      </defs>
      <path
        d={line}
        fill="none"
        stroke="#447a70"
        strokeWidth="2"
        markerEnd={marker}
        markerStart={
          link.state.toElementId === "lookup_value" ? marker : undefined
        }
      />
    </g>
  );
}

const config = {
  elementConfig: {
    components: {
      step: { component: NodeCard, minSize: { width: 188, height: 76 } },
    },
  },
  linkConfig: {
    components: { edge: { component: Edge, hasArrow: true, showName: false } },
  },
  toolbarConfig: [],
  globalConfig: { gridSize: 16 },
};
const positions: Record<string, Point> = {
  catalog: { x: 34, y: 20 },
  generate: { x: 34, y: 129 },
  lookup_value: { x: 311, y: 129 },
  submit: { x: 34, y: 238 },
  learn: { x: 311, y: 238 },
};

function stepInfo(step: Step) {
  if (step.tool === "lookup_value") {
    const finding = step.receipt.values?.length
      ? step.receipt.values.join(", ")
      : "未找到已核实的值";
    return (
      step.args.table +
      "." +
      step.args.column +
      " · " +
      step.args.term +
      " → " +
      finding
    );
  }
  if (step.tool === "submit_answer")
    return "原库实际返回 " + step.receipt.row_count + " 行";
  const preview = (step.receipt.sample || [])
    .slice(0, 2)
    .map((row) => row.join(" · "))
    .join(" / ");
  return preview || (step.receipt.accepted ? "SQL 执行成功" : "查询需要修订");
}

export default function HarnessPage() {
  const editor = useRef<VisualDiagramHandle>(null);
  const [lookup, setLookup] = useState(true);
  const [mode, setMode] = useState<Mode>("replay");
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const busy = job !== null;
  const [error, setError] = useState("");
  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const ticket = query.get("run"),
      saved = query.get("mode"),
      arm = query.get("lookup");
    if (
      ticket &&
      /^[0-9a-f]{32}$/.test(ticket) &&
      (saved === "live" || saved === "replay") &&
      (arm === "on" || arm === "off")
    ) {
      setLookup(arm === "on");
      setMode(saved);
      setJob({ ticket, mode: saved, operation: "status" });
    }
  }, []);
  useEffect(() => {
    const nodes = workflow.nodes.filter(
      (n) => lookup || n.id !== "lookup_value",
    );
    const edges = workflow.edges.filter(
      (pair) => lookup || !pair.includes("lookup_value"),
    );
    editor.current?.setView({
      id: "cosql-harness",
      elements: nodes.map((n) => ({
        id: n.id,
        name: n.label,
        type: "step",
        color: "#347766",
        select: false,
        locked: false,
        ...positions[n.id],
      })),
      links: edges.map(([from, to]) => ({
        id: from + "-" + to,
        name: "",
        type: "edge",
        color: "#447a70",
        points: [],
        fromElementId: from,
        toElementId: to,
      })),
    });
  }, [lookup]);
  useEffect(() => {
    if (!job) return;
    const { ticket, mode: runMode } = job;
    const controller = new AbortController();
    async function readJob(operation: string) {
      try {
        const view = editor.current?.getView();
        if (!view || view.id !== "cosql-harness" || !view.elements.length)
          throw Error("画布尚未就绪");
        const deadline = Date.now() + 360000;
        while (!controller.signal.aborted && Date.now() < deadline) {
          const structure = JSON.stringify({
            ...view,
            ticket,
            mode: runMode,
            operation,
          });
          const resp = await fetch(endpoint, {
            method: "POST",
            signal: controller.signal,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              flowId:
                "@com.horsecoder.common.whuTalent.dataTesting.core/FLOW_RUN",
              params: { structure },
            }),
          });
          const outer = await resp.json();
          if (!resp.ok || outer.result !== true || outer.status !== 200)
            throw Error(outer.message || "Java 流程调用失败");
          const next: Receipt = JSON.parse(outer.data.value.result);
          if (next.ticket !== ticket) throw Error("运行编号与请求不符");
          if (controller.signal.aborted) return;
          setReceipt(next);
          if (next.state === "complete") return;
          if (next.state === "failed") throw Error(next.error || "调度失败");
          operation = "status";
          await new Promise((resolve) => setTimeout(resolve, 850));
        }
        throw Error("读取回执超时");
      } catch (err) {
        if (!controller.signal.aborted)
          setError(err instanceof Error ? err.message : "调用失败");
      } finally {
        if (!controller.signal.aborted) setJob(null);
      }
    }
    void readJob(job.operation);
    return () => controller.abort();
  }, [job]);
  function dispatch() {
    if (busy) return;
    const ticket = crypto.randomUUID().replaceAll("-", "");
    const query = new URLSearchParams({
      run: ticket,
      mode,
      lookup: lookup ? "on" : "off",
    });
    window.history.replaceState(
      null,
      "",
      window.location.pathname + "?" + query,
    );
    setError("");
    setReceipt(null);
    setJob({ ticket, mode, operation: "start" });
  }
  const completed = receipt?.state === "complete" ? receipt : null;
  const last = completed?.actions[completed.actions.length - 1];
  return (
    <main className="h-workbench">
      <header className="h-top">
        <b>RSI Data Platform</b>
        <span> / </span>CoSQL 实验台 <code>rsi-data-platform</code>
      </header>
      <section className="h-title">
        <div>
          <h1>CoSQL 查询编排</h1>
          <p>
            联合航空有哪些始发机场？ <code>dev36_turn1 · flight_2</code>
          </p>
        </div>
        <div className="h-controls">
          <select
            aria-label="运行方式"
            value={mode}
            disabled={busy}
            onChange={(e) => {
              setMode(e.target.value as Mode);
              setReceipt(null);
              setError("");
            }}
          >
            <option value="replay">记录动作复跑</option>
            <option value="live">模型现场生成</option>
          </select>
          <button disabled={busy} onClick={dispatch}>
            {busy ? "正在调度…" : "运行编排"}
          </button>
        </div>
      </section>
      <div className="h-grid">
        <section className="h-panel">
          <div className="h-head">
            <h2>Harness 画布</h2>
            <label>
              <input
                type="checkbox"
                checked={lookup}
                disabled={busy}
                onChange={(e) => {
                  setLookup(e.target.checked);
                  setReceipt(null);
                  setError("");
                }}
              />{" "}
              启用专用查值工具
            </label>
          </div>
          <div className="h-canvas">
            <VisualDiagram ref={editor} canvasConfig={config} />
          </div>
          <div className="h-foot">
            <span>rsi-data-visual</span>
            <span>
              {lookup ? "5 个节点 · 4 条连线" : "4 个节点 · 3 条连线"}
            </span>
          </div>
        </section>
        <section className="h-panel h-results">
          <div className="h-head">
            <h2>调度回执</h2>
            <span role="status" className={completed?.set_match ? "h-ok" : ""}>
              {error
                ? "失败"
                : completed
                  ? completed.set_match
                    ? "机场集合一致"
                    : "机场集合有差异"
                  : busy
                    ? "执行中"
                    : "待运行"}
            </span>
          </div>
          {error && (
            <div role="alert" className="h-err">
              {error}
            </div>
          )}
          {completed ? (
            <>
              <div className="h-metrics">
                <div>
                  <b>{completed.checks_used} / 3</b>
                  <span>内部试查</span>
                </div>
                <div>
                  <b>{completed.rows}</b>
                  <span>结果行数</span>
                </div>
                <div>
                  <b>{completed.memory_writes}</b>
                  <span>记忆写入</span>
                </div>
              </div>
              <ol>
                {completed.actions.map((s, i) => (
                  <li key={i}>
                    <code>{s.tool}</code>
                    <small>{stepInfo(s)}</small>
                  </li>
                ))}
              </ol>
              <details>
                <summary>最终执行的 SQL</summary>
                <pre>{last?.args.sql}</pre>
              </details>
              <div className="h-id">
                运行编号 <code>{completed.ticket.slice(0, 12)}</code> ·{" "}
                {completed.mode === "live" ? "现场生成" : "记录动作复跑"}
              </div>
            </>
          ) : (
            !error && (
              <p className="h-empty">
                {busy
                  ? "调度器已接收画布，正在等待工具回执。"
                  : "运行后显示该次试查、SQL 和原库核对结果。"}
              </p>
            )
          )}
        </section>
      </div>
    </main>
  );
}
