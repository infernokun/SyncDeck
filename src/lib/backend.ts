import { call } from '@decky/api';

import type {
  BackendResult,
  ConnectionStatus,
  DaemonStatus,
  GameListResult,
  LibraryChange,
  PathSuggestions,
  StatusMap,
} from './types';

/**
 * Unwraps the backend's {ok, data, error} envelope into a value or a throw,
 * so callers can use plain try/catch instead of checking `ok` everywhere.
 */
async function invoke<T>(method: string, ...args: unknown[]): Promise<T> {
  const result = await call<unknown[], BackendResult<T>>(method, ...args);
  if (!result?.ok) {
    const error = result?.error;
    throw Object.assign(new Error(error?.message ?? 'Backend call failed'), {
      code: error?.code ?? 'internal',
      backend: error,
    });
  }
  return result.data as T;
}

export const backend = {
  getStatus: () => invoke<ConnectionStatus>('get_status'),
  reconnect: () => invoke<ConnectionStatus>('reconnect'),
  setSyncthingConfig: (mode: 'auto' | 'manual', baseUrl = '', apiKey = '') =>
    invoke<ConnectionStatus>('set_syncthing_config', mode, baseUrl, apiKey),
  setTargetDevices: (deviceIds: string[]) => invoke<string[]>('set_target_devices', deviceIds),

  listGames: () => invoke<GameListResult>('list_games'),
  setSkipCloud: (skip: boolean) => invoke<boolean>('set_skip_cloud', skip),
  startSyncthing: () => invoke<DaemonStatus>('start_syncthing'),
  setSyncthingAutostart: (enabled: boolean) => invoke<DaemonStatus>('set_syncthing_autostart', enabled),
  suggestPaths: (appid: number) => invoke<PathSuggestions>('suggest_paths', appid),
  syncGame: (appid: number, savePath: string, source = 'manual') =>
    invoke<{ appid: number; folderId: string; path: string; isFlatpakPath: boolean }>(
      'sync_game',
      appid,
      savePath,
      source,
    ),
  unsyncGame: (appid: number, forgetPath = false) =>
    invoke<{ appid: number; removed: boolean }>('unsync_game', appid, forgetPath),
  syncStatus: (appids?: number[]) => invoke<StatusMap>('sync_status', appids ?? null),
  relocateGame: (appid: number) => invoke<{ appid: number; path: string }>('relocate_game', appid),
  forgetGame: (appid: number) => invoke<{ appid: number; removed: boolean }>('forget_game', appid),
  rescanGame: (appid: number) => invoke<{ folderId: string }>('rescan_game', appid),
  ignoreGame: (appid: number, ignored = true) => invoke<null>('ignore_game', appid, ignored),
  pollLibrary: () => invoke<LibraryChange>('poll_library'),
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
