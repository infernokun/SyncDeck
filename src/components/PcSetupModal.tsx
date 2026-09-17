import { ConfirmModal, DialogButton, Spinner, TextField } from '@decky/ui';
import { useEffect, useState } from 'react';

import { backend, errorMessage } from '../lib/backend';
import type { RemoteStatus } from '../lib/types';

interface Props {
  current?: RemoteStatus;
  closeModal?: () => void;
  onSaved: () => void;
}

const KEY_FILE = 'syncdeck-key.txt';

/**
 * Connect SyncDeck to the PC's Syncthing API so new folders are created
 * there with the matching Windows path, instead of waiting to be accepted.
 *
 * Typing a 32 character key on the on-screen keyboard is the worst part of
 * this, so the address is filled in from the live sync connection and the
 * key can be imported from a file dropped into a folder already syncing.
 */
export function PcSetupModal({ current, closeModal, onSaved }: Props) {
  const [baseUrl, setBaseUrl] = useState(current?.baseUrl ?? '');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [keyFile, setKeyFile] = useState<{ path: string; name?: string } | null>(null);

  useEffect(() => {
    if (!current?.baseUrl) {
      backend
        .detectPcUrl()
        .then((url) => url && setBaseUrl((existing) => existing || url))
        .catch(() => undefined);
    }
    backend
      .findPcKeyFile()
      .then(setKeyFile)
      .catch(() => undefined);
  }, [current?.baseUrl]);

  const importFromFile = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await backend.importPcKey();
      const warning = result.fromSyncedFolder
        ? ' Delete it on your PC too: it is in a synced folder, so it exists on both machines.'
        : '';
      setNote(`Imported from ${result.path} and deleted it here.${warning}`);
      onSaved();
      closeModal?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const status = await backend.setRemoteConfig(baseUrl, apiKey);
      if (status.configured && !status.connected) {
        setError(status.error?.message ?? 'Could not reach the PC. Check the address and that its GUI listens on the network.');
        return;
      }
      onSaved();
      closeModal?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ConfirmModal
      strTitle="PC Syncthing"
      strOKButtonText={busy ? 'Checking...' : baseUrl ? 'Save' : 'Remove'}
      strCancelButtonText="Cancel"
      bOKDisabled={busy}
      onOK={save}
      onCancel={() => closeModal?.()}
    >
      <div style={{ fontSize: '12px', opacity: 0.8, marginBottom: '10px' }}>
        On the PC, open Syncthing, then Actions, Settings, GUI. Set the listen address to 0.0.0.0:8384 and copy the
        API key. Then folders SyncDeck creates are added on the PC at the matching Windows path, and nothing needs
        accepting.
      </div>

      <div style={{ fontSize: '12px', opacity: 0.8, marginBottom: '6px' }}>
        Rather than typing the key: save it to a file into a folder this Deck already syncs, your home folder,
        ~/Downloads, or a USB stick. Named <b>{KEY_FILE}</b>, <b>.syncdeck-key</b> or <b>.syncthing</b>. Importing
        reads it and deletes it.
      </div>
      <DialogButton disabled={busy} onClick={() => void importFromFile()} style={{ marginBottom: '12px' }}>
        {keyFile ? `Import key from ${keyFile.path}` : 'Look for a key file'}
      </DialogButton>

      {busy && <Spinner />}

      <TextField
        label="PC Syncthing address"
        description="Filled in from the device you are already syncing with"
        value={baseUrl}
        onChange={(event) => setBaseUrl(event.target.value)}
      />
      <TextField
        label="API key"
        description={current?.configured ? 'Leave empty to keep the saved key' : 'From the PC Syncthing GUI settings'}
        bIsPassword
        value={apiKey}
        onChange={(event) => setApiKey(event.target.value)}
      />
      {note && <div style={{ marginTop: '8px', color: '#3fb950' }}>{note}</div>}
      {error && <div style={{ marginTop: '8px', color: '#ff7b72' }}>{error}</div>}
    </ConfirmModal>
  );
}
