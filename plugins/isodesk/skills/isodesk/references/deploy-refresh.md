# Refreshing a dev-registered MSIX layout

Load this when a **packaged** WinUI app shows pre-edit UI after a clean build, or when
`Add-AppxPackage -Register` fails. For unpackaged exes none of it applies: the build
output is what runs.

## Why a green build changes nothing

A packaged WinUI app registered for development does not run from your build output. It
runs from a loose layout, typically:

```
bin\<Platform>\<Config>\<TFM>\<RID>\AppX\
```

`Get-AppxPackage <name> | Select-Object InstallLocation` names it exactly. `dotnet build`
refreshes the `<RID>` folder *above* that and leaves `AppX\` alone, so the app keeps
launching yesterday's code with a successful build behind it. Nothing warns you.

Two obvious recoveries both fail:

- there is no `Deploy` target on the project (`MSB4057`), and
- `-p:GenerateAppxPackageOnBuild=true` writes an `.msix` into `AppPackages\` without
  refreshing the loose layout.

## Read the registration before you build

`InstallLocation` also tells you which **platform** the registration points at, and that
decides what you are allowed to copy into it:

```powershell
Get-AppxPackage *YourApp* | Select-Object PackageFullName, InstallLocation
```

Copying an x64 payload over a layout registered as x86 fails at re-register time with
`0x80073CF3` (package validation), and `-ForceUpdateFromAnyVersion` does **not** help —
it relaxes the version check, not the architecture one. Build the platform the
registration already uses, refresh that layout, re-register that layout. Changing
architecture means removing the registration first, not forcing an update over it.

## The refresh

1. Copy the fresh payload into `AppX\`:
   - the exe and your assemblies,
   - **`resources.pri`**,
   - the compiled **`.xbf`** files, under their source folders.

   `resources.pri` and the `.xbf` files are not optional extras. A page added since the
   last real deployment is absent from both, and it fails to load at runtime with nothing
   in the build output to warn you.

2. Re-register:

   ```powershell
   Add-AppxPackage -Register <...>\AppX\AppxManifest.xml -ForceUpdateFromAnyVersion
   ```

## When it says it is already registered

`0x80073CFB` — "the provided package is already installed" — pointing at the folder you
just refreshed is **not a failure to fix**. The registration already points at that
layout, so the copy in step 1 was the whole deployment and no re-register is needed. Stop
there and launch.

## Prove it afterwards

`session_launch_app` and `session_activate_app` report `module.module_path` and
`module.module_mtime` for the process they started, plus `install_location` for packaged
apps. Compare the mtime against your edit before drawing any conclusion from the pixels.

## Kill the app first

A running session app holds its binaries open, so the copy in step 1 blocks or fails on
the lock — sometimes retrying silently for minutes. `session_destroy` before rebuilding,
and if a previous host process died without tearing down, `session_list(include_orphans=true)`
then `session_reap` — an orphaned app on an invisible desktop holds the same locks with
nothing on screen to explain it.
