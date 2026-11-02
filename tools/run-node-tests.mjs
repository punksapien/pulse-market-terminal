import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";

const files = readdirSync("tests")
  .filter((name) => /^test_.*\.(?:js|cjs)$/.test(name))
  .sort();

let failures = 0;
for (const file of files) {
  const result = spawnSync(process.execPath, [`tests/${file}`], { stdio: "inherit" });
  if (result.status !== 0) failures += 1;
}

if (failures) {
  console.error(`${failures} Node test file(s) failed`);
  process.exit(1);
}
console.log(`${files.length} Node test files passed`);
