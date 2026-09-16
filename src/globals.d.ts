/**
 * SteamClient is injected by Steam itself and is not part of @decky/api.
 * Only the slice SyncDeck touches is declared here.
 */
declare const SteamClient:
  | {
      Downloads?: {
        RegisterForDownloadItems?: (
          callback: (isDownloading: boolean, items: Array<{ appid: number; completed: boolean }>) => void,
        ) => { unregister: () => void };
      };
    }
  | undefined;
