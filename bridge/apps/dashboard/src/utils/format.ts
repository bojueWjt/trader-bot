export function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2
  }).format(value);
}

export function formatPercent(value: number): string {
  const formatted = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 2,
    minimumFractionDigits: 2
  }).format(value);

  return `${formatted}%`;
}

export function getReconnectDelayMs(): number {
  return 10000;
}

export function normalizePath(path: string): string {
  if (!path) {
    return "/dashboard";
  }

  if (path === "/") {
    return "/dashboard";
  }

  if (/^\/(dashboard|orders|review|risk|reports|reports\/daily\/\d{4}-\d{2}-\d{2})$/.test(path)) {
    return path;
  }

  return "/dashboard";
}

export function getReportDateFromPath(path: string): string {
  const match = path.match(/^\/reports\/daily\/(\d{4}-\d{2}-\d{2})$/);

  if (!match) {
    return new Date().toISOString().slice(0, 10);
  }

  const [date] = match.slice(1);
  return date;
}

export function statusTone(status: string): "good" | "warning" | "danger" | "muted" {
  if (/healthy|running|ok|low|online|connected|normal/i.test(status)) {
    return "good";
  }

  if (/warning|elevated|paused|medium|degraded/i.test(status)) {
    return "warning";
  }

  if (/critical|blocked|offline|high|down|error/i.test(status)) {
    return "danger";
  }

  return "muted";
}
