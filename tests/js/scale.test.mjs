import assert from "node:assert/strict";
import test from "node:test";

import { editAmount, scaleServings } from "../../src/souschef/static/scale.mjs";

test("an edited amount is kept when servings change again", () => {
  let state = {
    servings: 4,
    lines: [
      { id: "paprika", amount: 2, perServing: 0.5, edited: false },
      { id: "salt", amount: 4, perServing: 1, edited: false },
    ],
  };

  state = scaleServings(state, 2);
  assert.deepEqual(
    state.lines.map((line) => line.amount),
    [1, 2],
  );

  state = editAmount(state, "paprika", 1.5);
  state = scaleServings(state, 3);

  const paprika = state.lines.find((line) => line.id === "paprika");
  const salt = state.lines.find((line) => line.id === "salt");
  assert.equal(paprika.amount, 1.5);
  assert.equal(paprika.edited, true);
  assert.equal(salt.amount, 3);
  assert.equal(salt.edited, false);
  assert.equal(state.servings, 3);
});
