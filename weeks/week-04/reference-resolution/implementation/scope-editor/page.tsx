import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import Canvas, { type Question } from "./canvas";
import type { Graph, Receipt } from "./flow";

export default async function Page() {
  const reports = path.resolve(process.cwd(), "../../../reports");
  const source = JSON.parse(await readFile(path.join(reports, "2026-11-26_指代消解任务盘点/抽测/plan.json"), "utf8"));
  const turns: Question[] = source["输入"]["86"];
  const directory = path.join(reports, "2026-11-26_范围承接实验/RSI回执");
  const names = await readdir(directory).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return [];
    throw error;
  });
  const saved = await Promise.all(names.filter(n => n.endsWith(".json")).sort().map(async name => {
    const receipt: Receipt = JSON.parse(await readFile(path.join(directory, name), "utf8"));
    const canvas: Graph = JSON.parse(await readFile(path.join(directory, "../画布运行", receipt.ticket, "canvas.json"), "utf8"));
    return {name: name.slice(0,-5), receipt, canvas};
  }));
  return <Canvas turns={turns} saved={saved} />;
}
