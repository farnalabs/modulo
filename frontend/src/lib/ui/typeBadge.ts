export function typeBadgeClass(type: string): string {
  const map: Record<string, string> = {
    pipeline_template: "badge badge-context-blue",
    workflow: "badge badge-context-teal",
    agent: "badge badge-context-purple",
    schema: "badge badge-context-amber",
    integration: "badge badge-context-cyan",
    test_fixture: "badge badge-context-pink",
    composite: "badge badge-context-green",
    lifecycle_map: "badge badge-context-blue",
  };
  return map[type] ?? "badge badge-context-slate";
}
