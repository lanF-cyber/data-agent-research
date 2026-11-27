import { readFile } from "node:fs/promises";
import path from "node:path";
import Link from "next/link";
import "./versions.css";

type Table = { columns: string[]; rows: (string | number | null)[][]; sql: string };
type Delivery = Table & { ticket: string };
type Case = {
  id: string; database: string; users: [string, string, string | null][];
  question_zh: string; context_role: string; failure: string; expected: Table[]; before: Delivery; after: Delivery;
  published: Table & { same_user_contract: boolean; passes: [boolean, boolean] };
};
type Bundle = {
  cases: Case[]; failed: Record<string, string | number>[];
  summary: { 同题整段重跑: { 对话数: number; 目标: { 题数: number; 旧版对: number; 新版对: number; 错转对: number } };
    发布参考敏感性: { 发布输入相同: number; 发布参考仍错转对: number } };
};

function Result({ title, table }: { title: string; table: Table }) {
  return <div><h4>{title} <small>{table.rows.length} 行</small></h4>
    <pre>{table.sql}</pre>
    <div className="rr-scroll"><table><thead><tr>{table.columns.map((name,i) => <th key={i}>{name}</th>)}</tr></thead>
      <tbody>{table.rows.map((row,i) => <tr key={i}>{row.map((v,j) => <td key={j}>{v === null ? "NULL" : String(v)}</td>)}</tr>)}</tbody>
    </table></div>
  </div>;
}

export default async function Page() {
  const file = path.join(process.cwd(), "app/harness/versions/cases.json");
  const pack: Bundle = JSON.parse(await readFile(file, "utf8"));
  const batch = pack.summary.同题整段重跑;
  const published = pack.summary.发布参考敏感性;
  return <main className="rr">
    <nav><Link href="/harness">RSI Data Platform · CoSQL 实验台</Link></nav>
    <h1>历代 Harness 的修复记录</h1>
    <p>{batch.对话数} 段连续对话，同一批 {batch.目标.题数} 个目标问次，旧版通过 {batch.目标.旧版对} 个，v3 通过 {batch.目标.新版对} 个。
      其中 {batch.目标.错转对} 个从错到对。另有 1 个在固定错误前文时通过人工选择来源轮修回。</p>
    <p>{published.发布输入相同} 道修复的完整用户输入与 CoSQL 发布版相同，其中 {published.发布参考仍错转对} 道在两套参考下均为错转对。</p>
    <h2>{pack.cases.length} 道同输入修复</h2>
    {pack.cases.map(c => <details key={c.id}>
      <summary><code>{c.id}</code> {c.question_zh}</summary>
      <div className="rr-body"><p>{c.failure}</p><p><b>上下文</b>　{c.context_role}</p>
        <p><b>原库</b>　<code>{c.database}</code></p>
        <ol>{c.users.map(([id,first,follow]) => <li key={id}>{first}{follow && <span> → {follow}</span>}</li>)}</ol>
        <div className="rr-tables"><Result title="修复前" table={c.before} /><Result title="修复后" table={c.after} />
          <Result title="原库期望" table={c.expected[0]} /></div>
        <p><b>按发布参考复算</b>　旧版{c.published.passes[0] ? "通过" : "失分"}，新版{c.published.passes[1] ? "通过" : "失分"}。
          {c.published.same_user_contract ? "用户输入与发布版一致。" : "更早的用户输入经过修订。"}</p>
        <p className="rr-tickets">RSI 运行号　<code>{c.before.ticket}</code> → <code>{c.after.ticket}</code></p>
      </div>
    </details>)}
    <h2>此前 RSI 对照中的失分与回退</h2>
    <p>后续 Python 缺项实验已修回 dev252 的前两问，均从 20,662 人收回到 2,775 人。
      表中的 dev106_turn2、dev238_turn2、dev238_turn3 在后续实跑中仍然失分。</p>
    <table><thead><tr><th>问次</th><th>结果</th><th>本轮原句</th><th>期望 / 新版行数</th></tr></thead>
      <tbody>{pack.failed.map(c => <tr key={c["题号"]}><td><code>{c["题号"]}</code></td><td>{c["判定"]}</td>
        <td>{c["原句"]}</td><td>{c["期望行数"]} / {c["新版行数"]}</td></tr>)}</tbody>
    </table>
  </main>;
}
