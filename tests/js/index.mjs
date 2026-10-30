// This Node runs `node --test tests/js/` as the directory, so load every test file.
import { readdir } from "node:fs/promises";

const dir = new URL("./", import.meta.url);
const names = (await readdir(dir)).filter((name) => name.endsWith(".test.mjs")).sort();
for (const name of names) {
  await import(new URL(name, dir).href);
}
