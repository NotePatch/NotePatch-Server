export function formatDate(value?: string | null): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

export function formatBytes(value?: number | null): string {
  if (value === undefined || value === null) return "-";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let current = value / 1024;
  for (const unit of units) {
    if (current < 1024) return `${current.toFixed(current >= 10 ? 1 : 2)} ${unit}`;
    current /= 1024;
  }
  return `${current.toFixed(1)} PB`;
}

export function compactId(value?: string | null): string {
  if (!value) return "-";
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}

export function jsonText(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}
