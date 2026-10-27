/** An explicit URL always wins over the server's initial run selection. */
export function startupRoute(route, initialRun) {
  if (Object.keys(route).length || initialRun == null) return route;
  return { view: "run", key: initialRun };
}

/** An action is addressed by its zero-based ordinal inside a recorded step. */
export function actionRoute(key, step, action) {
  return { view: "run", key, tab: "trajectory", step, action };
}

export function selectedActionIndex(value) {
  if (value === undefined || value === null || !/^\d+$/.test(String(value))) return null;
  const index = Number(value);
  return Number.isSafeInteger(index) ? index : null;
}
