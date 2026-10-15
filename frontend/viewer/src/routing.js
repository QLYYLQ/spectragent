/** An explicit URL always wins over the server's initial run selection. */
export function startupRoute(route, initialRun) {
  if (Object.keys(route).length || initialRun == null) return route;
  return { view: "run", key: initialRun };
}
