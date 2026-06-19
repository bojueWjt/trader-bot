import { useEffect, useState } from "react";
import { dashboardStreamUrl } from "../utils/api";
import { getReconnectDelayMs } from "../utils/format";

export type RealtimeState = {
  connected: boolean;
  label: string;
};

export function useRealtimeConnection(onResync?: () => void, enabled = true): RealtimeState {
  const [state, setState] = useState<RealtimeState>({
    connected: false,
    label: "实时连接断开"
  });

  useEffect(() => {
    if (!enabled) {
      setState({
        connected: false,
        label: "实时连接断开"
      });
      return;
    }

    let source: EventSource | false = false;
    let timer: number | false = false;
    let disposed = false;

    function requestResync(): void {
      if (!onResync) {
        return;
      }

      onResync();
    }

    function scheduleReconnect(): void {
      if (disposed) {
        return;
      }

      setState({
        connected: false,
        label: "实时连接断开"
      });

      requestResync();
      timer = window.setTimeout(connect, getReconnectDelayMs());
    }

    function connect(): void {
      if (disposed) {
        return;
      }

      if (timer) {
        window.clearTimeout(timer);
        timer = false;
      }

      if (typeof EventSource === "undefined") {
        scheduleReconnect();
        return;
      }

      source = new EventSource(dashboardStreamUrl());

      source.onopen = () => {
        setState({
          connected: true,
          label: "实时连接正常"
        });
      };

      source.addEventListener("heartbeat", () => {
        setState({
          connected: true,
          label: "实时连接正常"
        });
      });

      source.addEventListener("dashboard_snapshot", () => {
        setState({
          connected: true,
          label: "实时连接正常"
        });
        requestResync();
      });

      source.onerror = () => {
        if (source) {
          source.close();
          source = false;
        }

        scheduleReconnect();
      };
    }

    connect();

    return () => {
      disposed = true;

      if (timer) {
        window.clearTimeout(timer);
      }

      if (source) {
        source.close();
      }
    };
  }, [enabled, onResync]);

  return state;
}
