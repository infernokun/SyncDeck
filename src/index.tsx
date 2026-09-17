import { addEventListener, removeEventListener, definePlugin, toaster } from '@decky/api';
import { ButtonItem, PanelSection, PanelSectionRow, staticClasses } from '@decky/ui';
import { useCallback, useEffect, useRef, useState } from 'react';
import { FaSync } from 'react-icons/fa';

import { ConnectionPanel } from './components/ConnectionPanel';
import { GameList } from './components/GameList';
import { backend, errorMessage } from './lib/backend';
import type { ConnectionStatus, GameListResult, LibraryChange, StatusMap } from './lib/types';

const STATUS_POLL_MS = 5000;

function Content() {
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [list, setList] = useState<GameListResult>({ games: [], hiddenCloud: 0, skipCloudSaves: true, total: 0, unavailable: [] });
  const [statuses, setStatuses] = useState<StatusMap>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const syncedAppids = useRef<number[]>([]);

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextList] = await Promise.all([backend.getStatus(), backend.listGames()]);
      setStatus(nextStatus);
      setList(nextList);
      syncedAppids.current = nextList.games.filter((game) => game.synced).map((game) => game.appid);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // The backend notices Syncthing coming or going on its 60s poll; refresh
  // the panel when it does so a Start press or a crash shows up unprompted.
  useEffect(() => {
    const onConnectionChanged = () => void refresh();
    addEventListener<[{ connected: boolean }]>('syncdeck/connection_changed', onConnectionChanged);
    return () => removeEventListener('syncdeck/connection_changed', onConnectionChanged);
  }, [refresh]);

  // Poll folder status only for games we actually sync, and only while the
  // QAM panel is mounted - there is no reason to hit Syncthing otherwise.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (syncedAppids.current.length === 0) return;
      try {
        const next = await backend.syncStatus(syncedAppids.current);
        if (!cancelled) setStatuses(next);
      } catch {
        // Transient; the connection panel already surfaces hard failures.
      }
    };
    void tick();
    const handle = window.setInterval(() => void tick(), STATUS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, [list]);

  return (
    <>
      {error && (
        <PanelSection>
          <PanelSectionRow>
            <div style={{ fontSize: '12px', color: '#ff7b72' }}>{error}</div>
          </PanelSectionRow>
        </PanelSection>
      )}

      <ConnectionPanel status={status} loading={loading} onRefresh={() => void refresh()} onError={setError} />

      <GameList
        games={list.games}
        statuses={statuses}
        unavailable={list.unavailable}
        hiddenCloud={list.hiddenCloud}
        skipCloud={list.skipCloudSaves}
        onChanged={() => void refresh()}
        onError={setError}
      />

      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void refresh()}>
            Refresh
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // Backend-side detection: catches installs that finished while the QAM
  // was closed. The frontend hook below catches them live.
  const onLibraryChanged = (change: LibraryChange) => {
    for (const game of change.newUnmapped.slice(0, 3)) {
      toaster.toast({
        title: 'SyncDeck',
        body: `${game.name} installed. Set up save syncing?`,
        icon: <FaSync />,
      });
    }
  };
  addEventListener<[LibraryChange]>('syncdeck/library_changed', onLibraryChanged);

  // SteamClient has no "app installed" event, but a download item flipping
  // to completed is the same signal.
  const downloadHook = SteamClient?.Downloads?.RegisterForDownloadItems?.(
    (_isDownloading: boolean, items: Array<{ appid: number; completed: boolean }>) => {
      if (items.some((item) => item.completed)) void backend.pollLibrary().catch(() => undefined);
    },
  );

  return {
    name: 'SyncDeck',
    titleView: <div className={staticClasses.Title}>SyncDeck</div>,
    content: <Content />,
    icon: <FaSync />,
    onDismount() {
      removeEventListener('syncdeck/library_changed', onLibraryChanged);
      downloadHook?.unregister?.();
    },
  };
});
