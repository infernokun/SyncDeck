import { ButtonItem, ConfirmModal, Focusable, PanelSection, PanelSectionRow, ToggleField, showModal } from '@decky/ui';
import { useState } from 'react';

import { backend, errorMessage } from '../lib/backend';
import { shortenPath } from '../lib/format';
import type { Game, StatusMap, UnavailableGame } from '../lib/types';
import { SavePathModal } from './SavePathModal';

interface Props {
  games: Game[];
  statuses: StatusMap;
  unavailable: UnavailableGame[];
  hiddenCloud: number;
  skipCloud: boolean;
  onChanged: () => void;
  onError: (message: string) => void;
}

function statusLine(game: Game, statuses: StatusMap): { text: string; color: string } {
  if (game.pathMissing) {
    return {
      text: game.relocateTo ? `Saves moved, found at ${game.relocateTo.label}` : 'Save folder missing at its old path',
      color: '#e3b341',
    };
  }
  if (!game.synced) {
    if (game.savePath) return { text: `Not syncing · ${shortenPath(game.savePath)}`, color: 'rgba(255,255,255,0.5)' };
    if (game.detected) return { text: `Detected: ${game.detected.label}`, color: '#e3b341' };
    if (!game.playedOnDeck) return { text: 'Not played on this Deck yet, no save folder to sync', color: 'rgba(255,255,255,0.45)' };
    return { text: 'No save folder found, pick one manually', color: 'rgba(255,255,255,0.5)' };
  }

  const status = statuses[String(game.appid)];
  if (!status) return { text: 'Syncing · checking...', color: 'rgba(255,255,255,0.6)' };
  if (status.error) return { text: status.error.message, color: '#ff7b72' };
  if (status.folderError) return { text: `Paused by Syncthing: ${status.folderError}`, color: '#e3b341' };
  if (status.errors > 0 || status.pullErrors > 0) {
    return { text: `${status.errors + status.pullErrors} sync error(s)`, color: '#ff7b72' };
  }
  // The other device has to accept a shared folder before anything syncs.
  if (status.awaitingAccept && status.awaitingAccept.length > 0) {
    const where = game.pcPath ? `. On the PC use ${game.pcPath}` : '';
    return {
      text: `Waiting for ${status.awaitingAccept.join(', ')} to accept the folder in Syncthing${where}`,
      color: '#e3b341',
    };
  }
  // A first scan of a big save folder can run for many minutes; say so
  // rather than showing a frozen-looking "syncing".
  if (status.scanning) return { text: 'Scanning folder (first scan can take a while)', color: '#58a6ff' };
  if (status.inSync) return { text: 'Up to date', color: '#3fb950' };
  return { text: `Syncing · ${status.needItems} item(s) left`, color: '#58a6ff' };
}

const REASON_TEXT: Record<UnavailableGame['reason'], (g: UnavailableGame) => string> = {
  library_missing: (g) => `Saves are on "${g.libraryLabel ?? 'a removed drive'}", which is not inserted. Syncing resumes when it is back. Nothing is deleted.`,
  game_missing: (g) =>
    g.driveOut
      ? `Game is on "${g.libraryLabel ?? 'a removed drive'}", which is not inserted. Its saves are here and still syncing.`
      : 'Game uninstalled. Its saves are still here and still syncing; reinstalling picks them up.',
  uninstalled: () => 'Uninstalled and the save folder is gone. Forget it, or leave it and it resumes on reinstall.',
  moved: (g) => (g.relocateTo ? `Saves moved, found at ${g.relocateTo.label}` : 'Save folder no longer exists at its old path.'),
};

export function GameList({ games, statuses, unavailable, hiddenCloud, skipCloud, onChanged, onError }: Props) {
  const [showAll, setShowAll] = useState(false);

  const synced = games.filter((game) => game.synced);
  const rest = games.filter((game) => !game.synced && !game.ignored);
  const visible = showAll ? [...synced, ...rest] : [...synced, ...rest.slice(0, 8)];
  const hidden = rest.length - (showAll ? rest.length : Math.min(rest.length, 8));

  const openPicker = (game: Game) => {
    showModal(<SavePathModal game={game} onSynced={onChanged} />);
  };

  const unsync = (game: Game) => {
    if (game.adopted) {
      // Not ours to delete; the picker still lets them change its path.
      openPicker(game);
      return;
    }
    // Confirm before removing the folder. No files are deleted either way,
    // but the PC side stops receiving updates.
    showModal(
      <ConfirmModal
        strTitle={`Stop syncing ${game.name}?`}
        strDescription="The Syncthing folder is removed on this Deck. No save files are deleted anywhere, and the path is remembered so re-enabling is one press."
        strOKButtonText="Stop syncing"
        strCancelButtonText="Keep syncing"
        onOK={() => {
          backend
            .unsyncGame(game.appid, false)
            .then(onChanged)
            .catch((err) => onError(errorMessage(err)));
        }}
      />,
    );
  };

  const confirmForget = (game: UnavailableGame) => {
    showModal(
      <ConfirmModal
        strTitle={`Forget ${game.name}?`}
        strDescription={
          game.adopted
            ? 'SyncDeck stops tracking this game. The Syncthing folder you created is left untouched.'
            : "The Syncthing folder SyncDeck created is removed on this Deck. No save files are deleted anywhere."
        }
        strOKButtonText={game.adopted ? 'Stop tracking' : 'Forget'}
        strCancelButtonText="Cancel"
        onOK={() => void forget(game.appid)}
      />,
    );
  };

  const relocate = async (appid: number) => {
    try {
      await backend.relocateGame(appid);
      onChanged();
    } catch (err) {
      onError(errorMessage(err));
    }
  };

  const forget = async (appid: number) => {
    try {
      await backend.forgetGame(appid);
      onChanged();
    } catch (err) {
      onError(errorMessage(err));
    }
  };

  const toggleSkipCloud = async (skip: boolean) => {
    try {
      await backend.setSkipCloud(skip);
      onChanged();
    } catch (err) {
      onError(errorMessage(err));
    }
  };

  return (
    <PanelSection title={`Games (${synced.length} syncing)`}>
      {visible.length === 0 && (
        <PanelSectionRow>
          <div style={{ fontSize: '12px', opacity: 0.7 }}>
            {hiddenCloud > 0
              ? `All ${hiddenCloud} installed games already use Steam Cloud.`
              : 'No installed games found.'}
          </div>
        </PanelSectionRow>
      )}

      {visible.map((game) => {
        const line = statusLine(game, statuses);
        const primary = () => {
          if (game.pathMissing && game.relocateTo) return void relocate(game.appid);
          if (game.pathMissing) return openPicker(game);
          return game.synced ? unsync(game) : openPicker(game);
        };
        return (
          <PanelSectionRow key={game.appid}>
            <Focusable
              style={{ display: 'flex', flexDirection: 'column', width: '100%' }}
              onSecondaryButton={() => openPicker(game)}
              onSecondaryActionDescription={game.synced ? 'Change path' : 'Pick path'}
            >
              <ButtonItem
                layout="below"
                onClick={primary}
                description={
                  <span style={{ color: line.color, fontSize: '11px' }}>
                    {game.adopted ? 'Existing Syncthing folder · ' : ''}
                    {line.text}
                    {game.hasSteamCloud ? ' · Steam Cloud' : ''}
                  </span>
                }
              >
                {game.name}
              </ButtonItem>
            </Focusable>
          </PanelSectionRow>
        );
      })}

      {hidden > 0 && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setShowAll(true)}>
            Show {hidden} more game{hidden === 1 ? '' : 's'}
          </ButtonItem>
        </PanelSectionRow>
      )}

      {unavailable.length > 0 && (
        <PanelSectionRow>
          <div style={{ fontSize: '12px', opacity: 0.75, marginTop: '8px' }}>Not available right now</div>
        </PanelSectionRow>
      )}
      {unavailable.map((game) => (
        <PanelSectionRow key={`unavailable-${game.appid}`}>
          <ButtonItem
            layout="below"
            onClick={() => (game.relocateTo ? void relocate(game.appid) : confirmForget(game))}
            description={<span style={{ fontSize: '11px', color: '#e3b341' }}>{REASON_TEXT[game.reason](game)}</span>}
          >
            {game.name}
            {game.relocateTo ? ': Relocate' : game.adopted ? ': Stop tracking' : ': Forget'}
          </ButtonItem>
        </PanelSectionRow>
      ))}

      <PanelSectionRow>
        <ToggleField
          label="Show Steam Cloud games"
          description={
            skipCloud && hiddenCloud > 0
              ? `${hiddenCloud} hidden, Steam already syncs their saves`
              : 'Games Valve syncs via Steam Cloud are hidden by default'
          }
          checked={!skipCloud}
          onChange={(show) => void toggleSkipCloud(!show)}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}
