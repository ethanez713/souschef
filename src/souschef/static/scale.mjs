export function scaleServings(state, nextServings) {
  var next = Number(nextServings);
  if (!Number.isFinite(next) || next <= 0 || next === state.servings) return state;
  return {
    servings: next,
    lines: state.lines.map(function (line) {
      if (line.edited) return line;
      var per = line.perServing;
      if (typeof per !== "number" || !Number.isFinite(per)) per = line.amount / state.servings;
      return Object.assign({}, line, { perServing: per, amount: per * next });
    }),
  };
}

export function editAmount(state, id, amount) {
  var value = Number(amount);
  return {
    servings: state.servings,
    lines: state.lines.map(function (line) {
      if (String(line.id) !== String(id)) return line;
      return Object.assign({}, line, { amount: value, edited: true });
    }),
  };
}
