import { ButtonItem, PanelSection, PanelSectionRow, Spinner, ToggleField } from '@decky/ui';

import { backend, errorMessage } from '../lib/backend';
import type { ConnectionStatus } from '../lib/types';

interface Props {
  status: ConnectionStatus | null;
  loading: boolean;
  onRefresh: () => void;
  onError: (message: string) => void;
}

export function ConnectionPanel({ status, loading, onRefresh, onError }: Props) {
  if (loading && !status) {
    return (
      <PanelSection title="Syncthing">
        <PanelSectionRow>
          <Spinner />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const daemon = status?.daemon;

  const startDaemon = async () => {
    try {
      await backend.startSyncthing();
      // Give the daemon a moment to bind its API port before we poll it.
      await new Promise((resolve) => setTimeout(resolve, 2500));
      await backend.reconnect();
      onRefresh();
    } catch (err) {
      onError(errorMessage(err));
    }
  };

  const setAutostart = async (enabled: boolean) => {
    try {
      await backend.setSyncthingAutostart(enabled);
      onRefresh();
    } catch (err) {
      onError(errorMessage(err));
    }
  };

  const autostartRow = daemon?.canManage ? (
    <PanelSectionRow>
      <ToggleField
        label="Start Syncthing automatically"
        description={
          daemon.sessionScoped
            ? 'Syncthing was started by Desktop Mode and will stop when you leave it. Turn this on so it runs in Gaming Mode on its own.'
            : 'Runs Syncthing at login in Gaming Mode too, so you never need Desktop Mode to start it'
        }
        checked={!!daemon.autostart}
        onChange={(value) => void setAutostart(value)}
      />
    </PanelSectionRow>
  ) : null;

  const sessionWarning = daemon?.sessionScoped && !daemon.autostart ? (
    <PanelSectionRow>
      <div style={{ fontSize: '12px', color: '#e3b341' }}>
        Started by SyncThingy's desktop autostart. It stops when you switch to Gaming Mode.
      </div>
    </PanelSectionRow>
  ) : null;

  if (!status?.connected) {
    const daemonDown = daemon && !daemon.running;
    return (
      <PanelSection title="Syncthing">
        <PanelSectionRow>
          <div style={{ fontSize: '13px', color: '#ff7b72' }}>
            {daemonDown
              ? 'Syncthing is not running.'
              : (status?.portConflict?.message ?? status?.error?.message ?? 'Not connected.')}
          </div>
        </PanelSectionRow>
        {daemonDown && daemon.canManage && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => void startDaemon()}>
              Start Syncthing
            </ButtonItem>
          </PanelSectionRow>
        )}
        {daemonDown && !daemon.canManage && (
          <PanelSectionRow>
            <div style={{ fontSize: '12px', opacity: 0.75 }}>
              SyncDeck could not work out how to launch it. Start Syncthing once from Desktop Mode.
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void backend.reconnect().then(onRefresh).catch((e) => onError(errorMessage(e)))}>
            Retry connection
          </ButtonItem>
        </PanelSectionRow>
        {autostartRow}
      </PanelSection>
    );
  }

  const remotes = (status.devices ?? []).filter((device) => !device.isLocal);
  const targets = new Set(status.targetDevices ?? []);

  const toggleTarget = async (deviceId: string, enabled: boolean) => {
    // An empty target list means "every paired device"; materialize it
    // before editing, or deselecting one device would change nothing.
    const next = new Set(targets.size === 0 ? remotes.map((device) => device.deviceId) : targets);
    enabled ? next.add(deviceId) : next.delete(deviceId);
    try {
      await backend.setTargetDevices([...next]);
      onRefresh();
    } catch (err) {
      onError(errorMessage(err));
    }
  };

  return (
    <PanelSection title="Syncthing">
      <PanelSectionRow>
        <div style={{ fontSize: '12px', opacity: 0.75 }}>
          Connected · {status.version} · {status.endpoint?.baseUrl}
        </div>
      </PanelSectionRow>
      {sessionWarning}

      {remotes.length === 0 ? (
        <PanelSectionRow>
          <div style={{ fontSize: '12px', color: '#e3b341' }}>
            No paired devices. Pair this Deck with your PC in Syncthing first.
          </div>
        </PanelSectionRow>
      ) : (
        remotes.map((device) => (
          <PanelSectionRow key={device.deviceId}>
            <ToggleField
              label={device.name}
              description="Share new game folders with this device"
              checked={targets.size === 0 || targets.has(device.deviceId)}
              onChange={(value) => void toggleTarget(device.deviceId, value)}
            />
          </PanelSectionRow>
        ))
      )}
      {autostartRow}
    </PanelSection>
  );
}
