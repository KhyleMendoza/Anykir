# AnyKir for Android

The phone side of AnyKir. It speaks the same protocol as the desktop app, so it works with any PC running
AnyKir or AnyKir Server.

- Control a shared PC: touchpad mode by default, plus touch mode, pinch to zoom, two-finger scroll and a
  floating button for the keyboard, shortcuts and Ctrl+Alt+Del.
- Share the phone so a PC can control it (needs the "AnyKir remote control" accessibility service turned on
  once).

Build:

```
flutter build apk --release
```

Release builds are signed with the key in `%USERPROFILE%\.anykir-signing\android.properties`. Without it
you get a debug-signed APK, which is fine for trying it out.

Check the protocol against a running desktop AnyKir:

```
dart run tool/interop_test.dart SERVER HOST:PORT PASSWORD
```
