"use client";

import { useState } from "react";
import { dispatch, type Arm, type Graph, type Receipt } from "./flow";
import "../harness.css";
import "./scope.css";

export type Question = { id: string; user: string };
const presets = [
  { n: 5, kind: "original", label: "第 5 轮 · 北美有哪些国家", source: 0, cue: "", output: "Which countries" },
  { n: 5, kind: "among", label: "改写 · 其中哪些国家在北美", source: 4, cue: "of those countries",
    question: "Which of those countries are in North America?", output: "Which of those countries" },
  { n: 6, kind: "original", label: "第 6 轮 · 这些国家的官方语言", source: 5, cue: "these countries", output: "official languages" },
  { n: 6, kind: "shown", label: "改写 · 刚才列出的三个国家", source: 5, cue: "the three countries you listed",
    question: "What are the official languages of just the three countries you listed?", output: "official languages" },
  { n: 3, kind: "original", label: "第 3 轮 · 那些语言的名字", source: 2, cue: "those languages", output: "names of those languages" },
];
const armNames = { full: "完整历史", reminder: "完整历史加范围提醒", selected: "手工选择历史" };
const caseNames: Record<string, string> = {reset: "独立查询北美", chain: "承接本组第 5 轮", among: "其中哪些在北美",
  languages: "加拿大的那些语言", "wrong-prefix": "承接旧三国错答", shown: "明确只看已展示的三国"};

function runLabel(name: string, r: Receipt) {
  const match = /^(output-)?(.+)-(\d+)-(full|reminder|selected)$/.exec(name);
  return match ? [caseNames[match[2]] ?? match[2], armNames[r.arm], match[1] ? "已标输出" : "未标输出",
    "第 " + match[3] + " 次"].join(" · ") : name;
}

export default function Canvas({turns, saved}: {turns: Question[]; saved: {name: string; receipt: Receipt; canvas: Graph}[]}) {
  const [index, setIndex] = useState(0);
  const [arm, setArm] = useState<Arm>("selected");
  const [source, setSource] = useState(0);
  const [carry, setCarry] = useState("set");
  const [cue, setCue] = useState("");
  const [output, setOutput] = useState(presets[0].output);
  const [parent, setParent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [activeGraph, setActiveGraph] = useState<Graph | null>(null);
  const [localParents, setLocalParents] = useState<Receipt[]>([]);
  const preset = presets[index];
  const question = preset.question ?? turns[preset.n-1].user;
  const parents = [...localParents, ...saved.map(r => r.receipt)].filter(r =>
    r.state === "complete" && r.arm === arm && r.case === "dev86_turn5" && r.variant === "original");
  function reset() { setReceipt(null); setActiveGraph(null); setError(""); }
  function previousReply(n: number, kind: string, strategy: Arm) {
    if (n !== 6 || kind !== "original") return "";
    return localParents.find(p => p.arm === strategy)?.ticket ??
      saved.find(s => s.name === "reset-1-" + strategy)?.receipt.ticket ?? "";
  }
  function selectCase(i: number) {
    const p = presets[i];
    setIndex(i); setSource(p.source); setCue(p.cue); setCarry(p.kind === "shown" ? "shown" : "set");
    setOutput(p.output); setParent(previousReply(p.n, p.kind, arm)); reset();
  }
  async function run() {
    setBusy(true); reset();
    const ticket = crypto.randomUUID().replaceAll("-", "");
    const elements = ["catalog", "generate", "lookup_value", "submit"].map(id => ({id}));
    const links: {fromElementId: string; toElementId: string; cue?: string}[] =
      [["catalog","generate"],["generate","lookup_value"],["lookup_value","generate"],["generate","submit"]]
        .map(([fromElementId, toElementId]) => ({fromElementId, toElementId}));
    function connect(id: string, phrase?: string) {
      elements.push({id});
      links.push({fromElementId: id, toElementId: "generate", ...(phrase ? {cue: phrase} : {})});
    }
    if (arm !== "selected") connect("history");
    else if (source) connect("t" + source + "_" + carry, cue.trim());
    if (output.trim()) connect("requested", output.trim());
    const canvas = {id: "cosql-harness", experiment: "scope", mode: "live", ticket,
      questionId: turns[preset.n-1].id, case: preset.kind, arm, parent, elements, links};
    try {
      let op = "start";
      const deadline = Date.now() + 8 * 60_000;
      while (Date.now() < deadline) {
        const r = await dispatch(canvas, op);
        if (r.state === "complete") {
          setReceipt(r); setActiveGraph(canvas);
          if (r.case === "dev86_turn5" && r.variant === "original") setLocalParents(p => [r, ...p]);
          return;
        }
        op = "status";
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      throw new Error("等待超过八分钟。运行编号 " + ticket);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function readTable(offset: number) {
    if (!activeGraph) return;
    setBusy(true); setError("");
    try { setReceipt(await dispatch(activeGraph, "status", offset)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  const missingCue = arm === "selected" && source > 0 && (!cue.trim() || !question.toLowerCase().includes(cue.trim().toLowerCase()));
  const badOutput = output.trim() && !question.toLowerCase().includes(output.trim().toLowerCase());
  return <main className="h-workbench s-work">
    <header className="h-top"><b>RSI Data Platform</b><span>/</span><a href="/harness">CoSQL 实验台</a></header>
    <section className="h-title"><div><h1>多轮查询范围</h1><p>CoSQL dev86 · world_1</p></div></section>
    <div className="s-grid">
      <section className="h-panel"><div className="h-head"><h2>人工编排</h2></div>
        <fieldset className="s-form" disabled={busy}>
          <label>当前问句<select value={index} onChange={e => selectCase(+e.target.value)}>
            {presets.map((p, i) => <option key={p.kind+p.n} value={i}>{p.label}</option>)}</select></label>
          <blockquote>{question}</blockquote>
          <label>历史策略<select value={arm} onChange={e => {const strategy = e.target.value as Arm;
            setArm(strategy); setParent(previousReply(preset.n, preset.kind, strategy)); reset();}}>
            {Object.entries(armNames).map(([id,n]) => <option key={id} value={id}>{n}</option>)}</select></label>
          {preset.n === 6 && <label>第 5 轮交付<select value={parent} onChange={e => {setParent(e.target.value); reset();}}>
            <option value="">已归档的原始回答（3 项）</option>
            {preset.kind === "original" && parents.map(p => <option key={p.ticket} value={p.ticket}>{p.rows} 项 · {p.ticket.slice(0,10)}</option>)}
          </select></label>}
          {arm === "selected" && <><label>引用轮次<select value={source} onChange={e => {setSource(+e.target.value); reset();}}>
            <option value={0}>本轮独立查询</option>{turns.slice(0,preset.n-1).map((t, i) =>
              <option key={t.id} value={i+1}>T{i+1} · {t.user}</option>)}</select></label>
            {source > 0 && <><label>承接内容<select value={carry} onChange={e => {setCarry(e.target.value); reset();}}>
              <option value="set">问句所指集合</option><option value="shown">实际展示的记录</option>
              <option value="operation">来源轮所做的操作</option></select></label>
              <label>本轮指代或省略的原文<input value={cue} onChange={e => {setCue(e.target.value); reset();}} /></label></>}
          </>}
          <label>要交付的字段（可留空）<input value={output} placeholder={preset.output}
            onChange={e => {setOutput(e.target.value); reset();}} /></label>
          <div className="s-edges" aria-label="生成节点的输入"><span>本轮原问</span>
            {arm !== "selected" ? <span>全部前文</span> : source > 0 && <span>T{source} · {carry}</span>}
            {output.trim() && <span>{output}</span>}<strong>→ SQL 生成 → 原库执行</strong></div>
          {Boolean(missingCue || badOutput) && <p role="alert">请从当前英文问句中选取完整片段。</p>}
          <div className="h-controls"><button disabled={busy || Boolean(missingCue || badOutput)} onClick={run}>
            {busy ? "模型查询中…" : "运行编排"}</button></div>
        </fieldset>
      </section>
      <section className="h-panel"><div className="h-head"><h2>原库回执</h2></div><div className="s-result">
        <label>已完成的对照<select disabled={busy} value={saved.find(s => s.receipt.ticket === receipt?.ticket)?.name ?? ""}
          onChange={e => {const s = saved.find(i => i.name === e.target.value);
            setReceipt(s?.receipt ?? null); setActiveGraph(s?.canvas ?? null); setError("");}}>
          <option value="">选择一份已保存的回执</option>{saved.map(s =>
            <option key={s.name} value={s.name}>{runLabel(s.name, s.receipt)}</option>)}</select></label>
        {error && <p role="alert">{error}</p>}
        {receipt && <><h3>{receipt.set_match ? "整表通过" : "整表未通过"} · {receipt.rows} 行</h3>
          <p>{receipt.question}</p><p>历史输入 {receipt.history_turns.map(n => "T"+n).join("、") || "空"}</p>
          <pre>{receipt.sql}</pre>{receipt.note && <p>{receipt.note}</p>}{receipt.followup && <p>{receipt.followup}</p>}
          <table><caption>{receipt.table_page ? "原库交付表 · 共 " + receipt.table_page.total + " 行" : "交付预览（前 8 行）"}</caption>
            <thead><tr>{receipt.columns.map((c,i) => <th key={i}>{c}</th>)}</tr></thead>
            <tbody>{(receipt.table_page?.rows ?? receipt.first_rows).map((row,i) =>
              <tr key={i}>{row.map((v,j) => <td key={j}>{v ?? "NULL"}</td>)}</tr>)}</tbody></table>
          <div className="h-controls">
            {!receipt.table_page && <button disabled={busy} onClick={() => readTable(0)}>查看完整交付表</button>}
            {receipt.table_page && receipt.table_page.offset > 0 &&
              <button disabled={busy} onClick={() => readTable(receipt.table_page!.offset - 100)}>上一页</button>}
            {receipt.table_page && receipt.table_page.total > receipt.table_page.offset + 100 &&
              <button disabled={busy} onClick={() => readTable(receipt.table_page!.offset + 100)}>下一页</button>}
          </div>
        </>}
      </div></section>
    </div>
  </main>;
}
