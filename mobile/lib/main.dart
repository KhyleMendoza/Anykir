import 'dart:async';
import 'dart:io';
import 'dart:math';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'protocol.dart';
import 'clipboard_sync.dart';
import 'host.dart';
import 'session.dart';
import 'updates.dart';
import 'version.dart';

const brand = Color(0xFF1D4ED8);

enum MouseMode { touchpad, buttons, touch }
const defaultServer = '192.168.210.112:7000, 100.123.9.43:7000';
const _oldDefaultServer = '192.168.210.112:7000';
const _tailscaleChannel = MethodChannel('anykir/tailscale');
const shortcuts = [
  ('Alt+Tab', ['alt', 'tab']),
  ('Win key', ['cmd']),
  ('Win+D (desktop)', ['cmd', 'd']),
  ('Win+E (explorer)', ['cmd', 'e']),
  ('Win+R (run)', ['cmd', 'r']),
  ('Ctrl+Shift+Esc (task manager)', ['ctrl', 'shift', 'esc']),
  ('Ctrl+Esc (start)', ['ctrl', 'esc']),
  ('Alt+F4', ['alt', 'f4']),
  ('Ctrl+C (copy)', ['ctrl', 'c']),
  ('Ctrl+V (paste)', ['ctrl', 'v']),
  ('Ctrl+Z (undo)', ['ctrl', 'z']),
  ('Print Screen', ['print_screen']),
];

final themeMode = ValueNotifier<ThemeMode>(ThemeMode.system);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final p = await SharedPreferences.getInstance();
  themeMode.value = ThemeMode.values.where((m) => m.name == p.getString('themeMode')).firstOrNull ?? ThemeMode.system;
  runApp(const AnyKirApp());
}

class AnyKirApp extends StatelessWidget {
  const AnyKirApp({super.key});
  @override
  Widget build(BuildContext context) => ValueListenableBuilder<ThemeMode>(
        valueListenable: themeMode,
        builder: (_, mode, __) => MaterialApp(
          title: 'AnyKir',
          debugShowCheckedModeBanner: false,
          themeMode: mode,
          theme: ThemeData(colorSchemeSeed: brand, useMaterial3: true),
          darkTheme: ThemeData(colorSchemeSeed: brand, brightness: Brightness.dark, useMaterial3: true),
          home: const HomePage(),
        ),
      );
}

Widget appearancePicker() => ValueListenableBuilder<ThemeMode>(
      valueListenable: themeMode,
      builder: (_, mode, __) => SegmentedButton<ThemeMode>(
        segments: const [
          ButtonSegment(value: ThemeMode.system, label: Text('Same as phone')),
          ButtonSegment(value: ThemeMode.light, label: Text('Light')),
          ButtonSegment(value: ThemeMode.dark, label: Text('Dark')),
        ],
        selected: {mode},
        showSelectedIcon: false,
        onSelectionChanged: (v) async {
          themeMode.value = v.first;
          (await SharedPreferences.getInstance()).setString('themeMode', v.first.name);
        },
      ),
    );

class ConnectPage extends StatefulWidget {
  const ConnectPage({super.key});
  @override
  State<ConnectPage> createState() => _ConnectPageState();
}

class _ConnectPageState extends State<ConnectPage> with WidgetsBindingObserver {
  final _server = TextEditingController(text: defaultServer);
  final _address = TextEditingController();
  final _password = TextEditingController();
  final _vault = const FlutterSecureStorage();
  bool _remember = true, _busy = false, _showPassword = false, _viewOnly = false;
  String _message = '', _onlineNote = 'Checking the server...';
  List<OnlinePc> _online = [];
  String? _liveServer, _tailscaleIp;
  bool _tailscaleChecked = false;
  Timer? _poll, _updatePoll;
  AppUpdate? _update;
  double? _updateProgress;
  String _updateNote = '';
  bool _inSession = false, _waitingForPermission = false;
  final Set<String> _autoTried = {};

  @override
  void initState() {
    super.initState();
    _load();
    _poll = Timer.periodic(const Duration(seconds: 3), (_) => _refresh());
    WidgetsBinding.instance.addObserver(this);
    listenInstallStatus((text) {
      if (mounted) setState(() => _updateNote = text);
    });
    _checkUpdate();
    _loadCanReopen();
    _updatePoll = Timer.periodic(const Duration(minutes: 10), (_) => _checkUpdate());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) return;
    _loadCanReopen();
    if (_waitingForPermission) {
      _waitingForPermission = false;
      _installUpdate();
    } else {
      _checkUpdate();
    }
  }

  bool _canReopen = true;

  Future<void> _loadCanReopen() async {
    final ok = await canReopen();
    if (mounted && ok != _canReopen) setState(() => _canReopen = ok);
  }

  Future<void> _checkUpdate() async {
    if (_updateProgress != null) return;
    final u = await checkForUpdate();
    if (!mounted || u == null) return;
    setState(() => _update = u);
    if (!_inSession && _autoTried.add(u.version)) _installUpdate();
  }

  Future<void> _installUpdate() async {
    final u = _update;
    if (u == null || _updateProgress != null) return;
    setState(() {
      _updateProgress = 0;
      _updateNote = 'Downloading...';
    });
    try {
      final path = await downloadUpdate(u, progress: (p) {
        if (mounted) setState(() => _updateProgress = p);
      });
      setState(() => _updateNote = 'Installing AnyKir ${u.version}...');
      final how = await installApk(path);
      _waitingForPermission = how == 'permission';
      if (mounted) {
        setState(() => _updateNote = _waitingForPermission
            ? 'Allow "Install unknown apps" for AnyKir, then come back - the update continues by itself'
            : 'Installing AnyKir ${u.version}. AnyKir restarts by itself when it is done.');
      }
    } catch (e) {
      if (mounted) setState(() => _updateNote = 'Update failed: $e');
    } finally {
      if (mounted) setState(() => _updateProgress = null);
    }
  }

  Widget _updateCard(BuildContext context) {
    final u = _update!;
    final busy = _updateProgress != null;
    return Card(
      color: Theme.of(context).colorScheme.primaryContainer,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 12, 12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(children: [
            const Icon(Icons.system_update, color: brand),
            const SizedBox(width: 12),
            Expanded(
              child: Text('Update available: AnyKir ${u.version}',
                  style: Theme.of(context).textTheme.titleSmall),
            ),
            FilledButton(onPressed: busy ? null : _installUpdate, child: const Text('Install')),
          ]),
          if (busy) ...[
            const SizedBox(height: 10),
            LinearProgressIndicator(value: _updateProgress == 0 ? null : _updateProgress),
          ],
          if (_updateNote.isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 8), child: Text(_updateNote)),
        ]),
      ),
    );
  }

  Future<void> _load() async {
    final p = await SharedPreferences.getInstance();
    final saved = p.getString('server');
    _server.text = (saved == null || saved == _oldDefaultServer) ? defaultServer : saved;
    _address.text = p.getString('address') ?? '';
    _remember = p.getBool('remember') ?? true;
    _viewOnly = p.getBool('viewOnly') ?? false;
    await _fillPassword();
    setState(() {});
    _refresh();
  }

  String _key(String address) {
    final (h, port) = parseAddress(address, defaultPort);
    return 'pw:${h.toLowerCase()}:$port';
  }

  Future<void> _fillPassword() async {
    if (_address.text.trim().isEmpty) return;
    final saved = await _vault.read(key: _key(_address.text));
    if (saved != null) _password.text = saved;
  }

  List<String> get _servers {
    final all = splitServers(_server.text);
    final live = _liveServer;
    if (live != null && all.remove(live)) all.insert(0, live);
    return all;
  }

  Future<void> _refresh() async {
    final ts = await phoneTailscaleIp();
    if (mounted) {
      setState(() {
        _tailscaleIp = ts;
        _tailscaleChecked = true;
      });
    }
    final servers = _servers;
    if (servers.isEmpty) {
      setState(() => _onlineNote = 'No AnyKir Server set');
      return;
    }
    try {
      final (server, list) = await fetchOnlineAny(servers);
      if (mounted) {
        setState(() {
          _liveServer = server;
          _online = list;
          _onlineNote = 'PCs online: ${list.length}';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _liveServer = null;
          _onlineNote = ts == null ? 'AnyKir Server not reachable - turn on Tailscale' : 'AnyKir Server not reachable';
        });
      }
    }
  }

  Future<void> _openTailscale() async {
    try {
      await _tailscaleChannel.invokeMethod('open');
    } catch (_) {
      if (mounted) setState(() => _message = 'Install Tailscale from the Play Store');
    }
  }

  Future<void> _pick(OnlinePc pc) async {
    _address.text = pc.address;
    _password.clear();
    await _fillPassword();
    setState(() {});
    if (_password.text.isNotEmpty) _connect();
  }

  Future<void> _connect() async {
    final address = _address.text.trim(), password = _password.text;
    if (address.isEmpty || password.isEmpty) {
      setState(() => _message = 'Enter the PC address and password');
      return;
    }
    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _message = 'Connecting...';
    });
    final p = await SharedPreferences.getInstance();
    await p.setString('server', _server.text.trim());
    await p.setString('address', address);
    await p.setBool('remember', _remember);
    await p.setBool('viewOnly', _viewOnly);
    final pc = matchPc(_online, address);
    try {
      final tailscale = _tailscaleIp == null ? null : pc?.tailscaleAddress;
      final s = await ViewerSession.open(
          address: address,
          password: password,
          servers: [if (_liveServer != null) _liveServer!, ..._servers.where((x) => x != _liveServer)],
          pcId: pc?.id,
          tailscale: tailscale);
      if (_remember) {
        await _vault.write(key: _key(address), value: password);
      } else {
        await _vault.delete(key: _key(address));
      }
      if (!mounted) return;
      setState(() => _message = '');
      _inSession = true;
      final reason = await Navigator.of(context).push<String>(MaterialPageRoute(
          builder: (_) => SessionPage(
              session: s,
              address: address,
              password: password,
              servers: _servers,
              tailscale: pc?.tailscaleAddress,
              title: pc?.name,
              viewOnly: _viewOnly)));
      _inSession = false;
      FocusManager.instance.primaryFocus?.unfocus();
      WidgetsBinding.instance.addPostFrameCallback((_) => FocusManager.instance.primaryFocus?.unfocus());
      if (mounted && reason != null) setState(() => _message = reason);
      _checkUpdate();
    } catch (e) {
      if (mounted) setState(() => _message = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _poll?.cancel();
    _updatePoll?.cancel();
    super.dispose();
  }

  Widget _tailscaleCard(BuildContext context) {
    final on = _tailscaleIp != null;
    final scheme = Theme.of(context).colorScheme;
    return Card(
      color: on ? Colors.green.withValues(alpha: 0.12) : scheme.surfaceContainerHighest,
      child: ListTile(
        leading: Icon(on ? Icons.vpn_lock : Icons.vpn_key_off, color: on ? Colors.green.shade700 : scheme.outline),
        title: Text(!_tailscaleChecked ? 'Tailscale: checking...' : (on ? 'Tailscale: On' : 'Tailscale: Off')),
        subtitle: Text(on
            ? 'This phone: $_tailscaleIp - PCs reachable from anywhere'
            : 'Turn it on to reach your PCs away from the office'),
        trailing: TextButton(onPressed: _openTailscale, child: Text(on ? 'Open' : 'Turn on')),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Row(children: [_Logo(size: 28), SizedBox(width: 10), Text('AnyKir')]),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: Chip(
              label: const Text('v$appVersion'),
              visualDensity: VisualDensity.compact,
              side: BorderSide.none,
              backgroundColor: _update != null
                  ? Theme.of(context).colorScheme.tertiaryContainer
                  : Theme.of(context).colorScheme.secondaryContainer,
            ),
          ),
        ],
      ),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.all(16), children: [
          if (_update != null) _updateCard(context),
          _tailscaleCard(context),
          const SizedBox(height: 12),
          Text('Connect to a PC', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 12),
          TextField(
            controller: _address,
            decoration: const InputDecoration(labelText: 'Address', hintText: '192.168.x.x:7001', border: OutlineInputBorder()),
            keyboardType: TextInputType.url,
            onChanged: (_) => _fillPassword(),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _password,
            obscureText: !_showPassword,
            decoration: InputDecoration(
              labelText: 'Password',
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(_showPassword ? Icons.visibility_off : Icons.visibility),
                onPressed: () => setState(() => _showPassword = !_showPassword),
              ),
            ),
            onSubmitted: (_) => _connect(),
          ),
          CheckboxListTile(
            value: _remember,
            onChanged: (v) => setState(() => _remember = v ?? true),
            title: const Text('Remember password on this phone'),
            contentPadding: EdgeInsets.zero,
            controlAffinity: ListTileControlAffinity.leading,
          ),
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: SegmentedButton<bool>(
              segments: const [
                ButtonSegment(value: false, icon: Icon(Icons.mouse_outlined), label: Text('Control')),
                ButtonSegment(value: true, icon: Icon(Icons.visibility_outlined), label: Text('View only')),
              ],
              selected: {_viewOnly},
              showSelectedIcon: false,
              onSelectionChanged: (v) => setState(() => _viewOnly = v.first),
            ),
          ),
          FilledButton.icon(
            onPressed: _busy ? null : _connect,
            icon: _busy ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.login),
            label: const Text('Connect'),
          ),
          if (_message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Text(_message, style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ),
          const SizedBox(height: 20),
          Row(children: [
            Expanded(child: Text(_onlineNote, style: Theme.of(context).textTheme.titleMedium)),
            IconButton(onPressed: _refresh, icon: const Icon(Icons.refresh)),
          ]),
          for (final pc in _online)
            Card(
              key: ValueKey('pc-${pc.id}'),
              child: ListTile(
                leading: const Icon(Icons.desktop_windows, color: brand),
                title: Text(pc.name),
                subtitle: Text([
                  pc.address,
                  if (pc.tailscaleAddress != null && pc.tailscaleAddress != pc.address) 'Tailscale ${pc.tailscaleAddress}',
                  if (pc.admin) 'admin + UAC',
                ].join('  •  ')),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => _pick(pc),
              ),
            ),
          const SizedBox(height: 16),
          ExpansionTile(
            title: const Text('Advanced'),
            tilePadding: EdgeInsets.zero,
            children: [
              const SizedBox(height: 10),
              TextField(
                controller: _server,
                decoration: const InputDecoration(
                    labelText: 'AnyKir Server',
                    helperText: 'Several allowed, separated by commas (LAN, then Tailscale)',
                    border: OutlineInputBorder()),
                onSubmitted: (_) => _refresh(),
              ),
              const SizedBox(height: 8),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Reopen after updates'),
                subtitle: Text(_canReopen
                    ? 'On - AnyKir opens again by itself after updating'
                    : 'Android needs "Display over other apps" for this. Otherwise tap the notification to open AnyKir.'),
                value: _canReopen,
                onChanged: (_) => allowReopen(),
              ),
              const SizedBox(height: 8),
              Align(alignment: Alignment.centerLeft, child: Text('Appearance', style: Theme.of(context).textTheme.labelLarge)),
              const SizedBox(height: 6),
              SizedBox(width: double.infinity, child: appearancePicker()),
              const SizedBox(height: 12),
              Row(children: [
                Expanded(child: Text('AnyKir $appVersion for Android', style: Theme.of(context).textTheme.bodySmall)),
                TextButton(
                  onPressed: () async {
                    await _checkUpdate();
                    if (mounted && _update == null) {
                      setState(() => _message = feedReached
                          ? 'AnyKir is up to date'
                          : "Couldn't reach the update server (check Wi-Fi or Tailscale) - try again later");
                    }
                  },
                  child: const Text('Check for updates'),
                ),
              ]),
            ],
          ),
        ]),
      ),
    );
  }
}

class SessionPage extends StatefulWidget {
  final ViewerSession session;
  final String address, password;
  final List<String> servers;
  final String? title, tailscale;
  final bool viewOnly;
  const SessionPage(
      {super.key,
      required this.session,
      required this.address,
      required this.password,
      required this.servers,
      this.tailscale,
      this.title,
      this.viewOnly = false});
  @override
  State<SessionPage> createState() => _SessionPageState();
}

class _SessionPageState extends State<SessionPage> with WidgetsBindingObserver {
  late ViewerSession s = widget.session;
  final _typing = TextEditingController(text: _sentinel);
  final _typingFocus = FocusNode();
  StreamSubscription<void>? _sub;
  bool _leaving = false, _reconnecting = false;
  bool _menuOpen = false, _ballDragging = false, _keysOpen = false, _keysMore = false;
  Offset _pill = const Offset(20, 8);
  bool _pillMini = false;
  double get _pillWidth => _pillMini ? 52 : 200;
  bool _autoKeyboard = true, _keysAuto = false, _lastCaret = false;
  DateTime _lastClickAt = DateTime(0);
  Timer? _caretCheck;
  Offset? _ball;
  StreamSubscription<String>? _noticeSub;
  Quality _quality = qualities.first;
  final Set<String> _mods = {};
  MouseMode _mode = MouseMode.touchpad;
  bool _fill = false, _landscape = true;
  double _pointerSpeed = 1.0, _scale = 1, _startDist = 0, _startScale = 1, _scrollRest = 0;
  Offset _pan = Offset.zero, _cur = const Offset(0.5, 0.5), _startFocal = Offset.zero, _prevFocal = Offset.zero;
  Rect _startRect = Rect.zero;
  Size _area = Size.zero;
  final Map<int, Offset> _pts = {};
  Offset? _downPos, _lastMovePos;
  DateTime _lastMoveAt = DateTime(0), _curTouched = DateTime(0), _twoStartAt = DateTime(0);
  bool _moved = false, _longPressed = false, _multi = false, _armed = false, _dragging = false;
  int _twoKind = 0;
  Timer? _longTimer, _pendingClick, _clipPoll, _twoTimer;
  StreamSubscription<String>? _clipSub;
  int? _clipStamp;
  bool _clipByStamp = true, _clipBusy = false, _foreground = true;
  Timer? _heal;
  String _phoneClip = '';
  static const _sentinel = '  ';

  @override
  void initState() {
    super.initState();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
    _applyOrientation();
    WidgetsBinding.instance.addObserver(this);
    s.viewOnly = widget.viewOnly;
    _attach();
    _loadPrefs();
    _startClipboard();
  }

  Future<void> _startClipboard() async {
    _clipStamp = await clipStamp();
    _clipByStamp = _clipStamp != null;
    if (!_clipByStamp) _phoneClip = await readClipText() ?? '';
    _clipPoll = Timer.periodic(const Duration(seconds: 1), (_) => _checkPhoneClip());
    _heal = Timer.periodic(const Duration(seconds: 5), (_) => _maybeHeal());
  }

  Future<void> _checkPhoneClip() async {
    if (!_foreground || _clipBusy || !s.clipboardAllowed) return;
    _clipBusy = true;
    try {
      String? text;
      if (_clipByStamp) {
        final stamp = await clipStamp();
        if (stamp == null || stamp <= 0 || stamp == _clipStamp) return;
        _clipStamp = stamp;
        text = await readClipText();
      } else {
        text = await readClipText();
        if (text == null || text == _phoneClip) return;
      }
      if (text == null || text.isEmpty) return;
      _phoneClip = text;
      if (s.sendClip(text)) _toast('Copied to the PC - paste there with Ctrl+V');
    } finally {
      _clipBusy = false;
    }
  }

  Future<void> _fromPc(String text) async {
    _phoneClip = text;
    await writeClipText(text);
    _clipStamp = await clipStamp() ?? _clipStamp;
  }

  Future<void> _paste() async {
    await _checkPhoneClip();
    s.combo(['ctrl', 'v']);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _foreground = state == AppLifecycleState.resumed;
    if (_foreground) {
      _checkPhoneClip();
      _refreshAll();
    }
  }

  DateTime _lastHeal = DateTime.now();
  bool _metered = false;
  int _healTicks = 0;

  void _refreshAll() {
    _lastHeal = DateTime.now();
    s.refresh();
  }

  Future<void> _maybeHeal() async {
    if (!_foreground || !mounted) return;
    if (_healTicks++ % 6 == 0) _metered = await meteredNetwork();
    if (healDue(DateTime.now(), _lastHeal, s.lastFrameAt, metered: _metered)) _refreshAll();
  }

  void _attach() {
    _sub?.cancel();
    _sub = s.changes.stream.listen((_) {
      if (!mounted) return;
      if (_pts.isEmpty && s.cursorX >= 0 && DateTime.now().difference(_curTouched).inMilliseconds > 900) {
        _cur = Offset(s.cursorX, s.cursorY);
      }
      if (s.caret != _lastCaret) {
        _lastCaret = s.caret;
        _caretChanged(s.caret);
      }
      setState(() {});
    });
    _noticeSub?.cancel();
    _noticeSub = s.notices.stream.listen(_toast);
    _clipSub?.cancel();
    _clipSub = s.clips.stream.listen(_fromPc);
    s.closed.future.then(_onClosed);
  }

  Future<void> _onClosed(String reason) async {
    if (_leaving || !mounted) return;
    setState(() => _reconnecting = true);
    final until = DateTime.now().add(const Duration(seconds: 60));
    while (!_leaving && mounted && DateTime.now().isBefore(until)) {
      await Future<void>.delayed(const Duration(seconds: 3));
      try {
        final pcId = s.pcId;
        var servers = widget.servers;
        var tailscale = widget.tailscale;
        if (pcId != null) {
          try {
            final (live, online) = await fetchOnlineAny(servers);
            final pc = online.where((p) => p.id == pcId).firstOrNull;
            if (pc == null) continue;
            servers = [live, ...servers.where((x) => x != live)];
            tailscale = pc.tailscaleAddress ?? tailscale;
          } catch (_) {
            servers = const [];
          }
        }
        if (await phoneTailscaleIp() == null) tailscale = null;
        if (pcId != null && servers.isEmpty && tailscale == null) continue;
        final fresh = await ViewerSession.open(
            address: widget.address,
            password: widget.password,
            servers: servers,
            pcId: pcId,
            tailscale: tailscale,
            quality: _quality);
        if (!mounted || _leaving) {
          fresh.close();
          return;
        }
        fresh.viewOnly = s.viewOnly;
        setState(() {
          s = fresh;
          _reconnecting = false;
        });
        _attach();
        return;
      } catch (_) {}
    }
    if (mounted && !_leaving) Navigator.of(context).pop('Connection lost: $reason');
  }

  Future<void> _disconnect() async {
    _leaving = true;
    s.close();
    if (mounted) Navigator.of(context).pop();
  }

  @override
  void dispose() {
    _leaving = true;
    _sub?.cancel();
    _noticeSub?.cancel();
    _clipSub?.cancel();
    _clipPoll?.cancel();
    _heal?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    s.close();
    _longTimer?.cancel();
    _pendingClick?.cancel();
    _twoTimer?.cancel();
    _caretCheck?.cancel();
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
    SystemChrome.setPreferredOrientations(const []);
    super.dispose();
  }

  void _sendKey(String keysym, {String char = ''}) {
    if (_mods.isNotEmpty) {
      s.combo([..._mods, keysym]);
      setState(_mods.clear);
    } else {
      s.key(keysym, char: char);
    }
  }

  void _onTyped(String value) {
    if (value.length < _sentinel.length) {
      for (var i = value.length; i < _sentinel.length; i++) {
        _sendKey('BackSpace');
      }
    } else if (value.length > _sentinel.length) {
      final added = value.substring(_sentinel.length);
      if (_mods.isNotEmpty && added.length == 1) {
        _sendKey(added.toLowerCase());
      } else {
        s.typeText(added);
      }
    }
    _typing.value = const TextEditingValue(text: _sentinel, selection: TextSelection.collapsed(offset: _sentinel.length));
  }

  void _toggleKeyboard() => _setKeyboard(!_keysOpen);

  Future<void> _setPillMini(bool mini) async {
    setState(() => _pillMini = mini);
    final p = await SharedPreferences.getInstance();
    await p.setBool('pillMini', mini);
  }

  void _flipPhoneKeyboard() {
    if (MediaQuery.viewInsetsOf(context).bottom > 0) {
      SystemChannels.textInput.invokeMethod('TextInput.hide');
    } else {
      _setKeyboard(true);
    }
  }

  Future<void> _savePill() async {
    final p = await SharedPreferences.getInstance();
    await p.setDouble('pillRight', _pill.dx);
    await p.setDouble('pillBottom', _pill.dy);
  }

  void _setKeyboard(bool open, {bool auto = false}) {
    if (!mounted || (open && s.viewOnly)) return;
    if (open) {
      _typingFocus.requestFocus();
      SystemChannels.textInput.invokeMethod('TextInput.show');
    } else {
      _typingFocus.unfocus();
    }
    setState(() {
      _keysOpen = open;
      _keysAuto = open && auto;
      if (open) _keysMore = false;
    });
  }

  void _clicked() {
    _lastClickAt = DateTime.now();
    if (!_autoKeyboard) return;
    _caretCheck?.cancel();
    _caretCheck = Timer(const Duration(milliseconds: 600), () {
      if (!_keysOpen && (s.cursorShape == 'ibeam' || s.caret)) {
        _setKeyboard(true, auto: true);
      }
    });
  }

  void _caretChanged(bool on) {
    if (!_autoKeyboard) return;
    final recent = DateTime.now().difference(_lastClickAt).inMilliseconds < 2500;
    if (on && recent && !_keysOpen) {
      _setKeyboard(true, auto: true);
    } else if (!on && _keysOpen && _keysAuto) {
      _setKeyboard(false);
    }
  }

  Rect _baseRect(Size area) {
    final w = s.width.toDouble(), h = s.height.toDouble();
    if (w <= 0 || h <= 0 || area.isEmpty) return Offset.zero & area;
    final k = _fill ? max(area.width / w, area.height / h) : min(area.width / w, area.height / h);
    return Rect.fromCenter(center: area.center(Offset.zero), width: w * k, height: h * k);
  }

  Rect _viewRect(Size area) {
    final b = _baseRect(area);
    final w = b.width * _scale, h = b.height * _scale;
    final cx = (area.width - w) / 2, cy = (area.height - h) / 2;
    double fix(double v, double size, double room) => size <= room ? (room - size) / 2 : v.clamp(room - size, 0.0);
    return Rect.fromLTWH(fix(cx + _pan.dx, w, area.width), fix(cy + _pan.dy, h, area.height), w, h);
  }

  void _setTopLeft(Offset topLeft, double scale) {
    final b = _baseRect(_area);
    _pan = topLeft - Offset((_area.width - b.width * scale) / 2, (_area.height - b.height * scale) / 2);
  }

  Offset _toRemote(Offset p, Rect r) =>
      Offset(((p.dx - r.left) / r.width).clamp(0.0, 1.0), ((p.dy - r.top) / r.height).clamp(0.0, 1.0));

  void _keepCursorVisible() {
    final r = _viewRect(_area);
    if (r.width <= _area.width + 1 && r.height <= _area.height + 1) return;
    final p = Offset(r.left + _cur.dx * r.width, r.top + _cur.dy * r.height);
    const m = 48.0;
    var dx = 0.0, dy = 0.0;
    if (p.dx < m) dx = m - p.dx;
    if (p.dx > _area.width - m) dx = _area.width - m - p.dx;
    if (p.dy < m) dy = m - p.dy;
    if (p.dy > _area.height - m) dy = _area.height - m - p.dy;
    if (dx != 0 || dy != 0) _setTopLeft(r.topLeft + Offset(dx, dy), _scale);
  }

  void _fit() => setState(() {
        _scale = 1;
        _pan = Offset.zero;
      });

  bool get _relative => _mode != MouseMode.touch;

  void _moveCursorTo(Offset n) {
    _cur = Offset(n.dx.clamp(0.0, 1.0), n.dy.clamp(0.0, 1.0));
    _curTouched = DateTime.now();
    s.mouse('move', _cur.dx, _cur.dy);
  }

  void _clickAtCursor({String button = 'left', int times = 1}) {
    for (var i = 0; i < times; i++) {
      s.click(_cur.dx, _cur.dy, button: button);
    }
    if (button == 'left') _clicked();
  }

  void _viewDown(PointerDownEvent e) {
    _pts[e.pointer] = e.localPosition;
    _lastMovePos = e.localPosition;
    if (_pts.length >= 2) {
      _multi = true;
      final ps = _pts.values.take(2).toList();
      _startDist = (ps[0] - ps[1]).distance;
      _startFocal = (ps[0] + ps[1]) / 2;
      _startScale = _scale;
      _startRect = _viewRect(_area);
    }
  }

  void _viewMove(PointerMoveEvent e) {
    if (!_pts.containsKey(e.pointer)) return;
    _pts[e.pointer] = e.localPosition;
    if (_pts.length == 1 && !_multi) {
      final delta = e.localPosition - (_lastMovePos ?? e.localPosition);
      _lastMovePos = e.localPosition;
      if (_scale > 1.001) setState(() => _setTopLeft(_viewRect(_area).topLeft + delta, _scale));
    } else if (_pts.length >= 2 && _startDist > 0) {
      final ps = _pts.values.take(2).toList();
      final dist = (ps[0] - ps[1]).distance;
      final focal = (ps[0] + ps[1]) / 2;
      final b = _baseRect(_area);
      final scale = (_startScale * dist / _startDist).clamp(1.0, 6.0);
      final n = Offset((_startFocal.dx - _startRect.left) / _startRect.width,
          (_startFocal.dy - _startRect.top) / _startRect.height);
      setState(() {
        _scale = scale < 1.03 ? 1 : scale;
        _setTopLeft(focal - Offset(n.dx * b.width * _scale, n.dy * b.height * _scale), _scale);
      });
    }
  }

  void _viewUp(PointerEvent e) {
    _pts.remove(e.pointer);
    if (_pts.isEmpty) _multi = false;
  }

  void _onDown(PointerDownEvent e) {
    if (_menuOpen) return;
    if (s.viewOnly) return _viewDown(e);
    _pts[e.pointer] = e.localPosition;
    final now = DateTime.now();
    if (_pts.length == 1) {
      _multi = false;
      _moved = false;
      _longPressed = false;
      _downPos = e.localPosition;
      _lastMovePos = e.localPosition;
      _lastMoveAt = now;
      _armed = _relative && _pendingClick != null && _pendingClick!.isActive;
      if (_armed) _pendingClick!.cancel();
      _longTimer?.cancel();
      _longTimer = Timer(const Duration(milliseconds: 480), _onLongPress);
    } else if (_pts.length == 2) {
      _longTimer?.cancel();
      if (_dragging) _endDrag();
      if (_armed) {
        _armed = false;
        _clickAtCursor();
      }
      _multi = true;
      _twoKind = 0;
      _twoStartAt = now;
      _twoTimer?.cancel();
      _twoTimer = Timer(const Duration(milliseconds: 450), _onTwoFingerHold);
      final ps = _pts.values.toList();
      _startDist = (ps[0] - ps[1]).distance;
      _startFocal = _prevFocal = (ps[0] + ps[1]) / 2;
      _startScale = _scale;
      _startRect = _viewRect(_area);
      _scrollRest = 0;
    }
  }

  void _onLongPress() {
    if (_pts.length != 1 || _moved || _multi || _downPos == null) return;
    _longPressed = true;
    _armed = false;
    HapticFeedback.mediumImpact();
    if (!_relative) {
      final p = _toRemote(_downPos!, _viewRect(_area));
      _cur = p;
      s.mouse('move', p.dx, p.dy);
    }
    s.mouse('down', _cur.dx, _cur.dy);
    _dragging = true;
    setState(() {});
  }

  void _onTwoFingerHold() {
    if (_pts.length < 2 || _twoKind != 0) return;
    _twoKind = 3;
    HapticFeedback.mediumImpact();
    _clickAtCursor(button: 'right');
  }

  void _onMove(PointerMoveEvent e) {
    if (s.viewOnly) return _viewMove(e);
    if (!_pts.containsKey(e.pointer)) return;
    _pts[e.pointer] = e.localPosition;
    final rect = _viewRect(_area);
    if (_pts.length == 1 && !_multi) {
      if (!_moved && (e.localPosition - _downPos!).distance > 10) {
        _moved = true;
        _longTimer?.cancel();
        if (_dragging) {
        } else if (!_relative) {
          final p = _toRemote(_downPos!, rect);
          _cur = p;
          s.mouse('move', p.dx, p.dy);
          s.mouse('down', p.dx, p.dy);
          _dragging = true;
        } else if (_armed) {
          s.mouse('down', _cur.dx, _cur.dy);
          _dragging = true;
        }
      }
      final now = DateTime.now();
      if (!_moved) {
        _lastMovePos = e.localPosition;
        _lastMoveAt = now;
        return;
      }
      if (!_relative) {
        final p = _toRemote(e.localPosition, rect);
        _cur = p;
        s.mouse('move', p.dx, p.dy);
        return;
      }
      final delta = e.localPosition - _lastMovePos!;
      final ms = max(1, now.difference(_lastMoveAt).inMilliseconds);
      _lastMovePos = e.localPosition;
      _lastMoveAt = now;
      final speed = delta.distance / ms;
      final gain = (0.65 + 1.4 * min(speed, 1.8)) * _pointerSpeed;
      _moveCursorTo(_cur + Offset(delta.dx * gain / rect.width, delta.dy * gain / rect.height));
      _keepCursorVisible();
      setState(() {});
    } else if (_pts.length >= 2) {
      final ps = _pts.values.take(2).toList();
      final dist = (ps[0] - ps[1]).distance;
      final focal = (ps[0] + ps[1]) / 2;
      if (_twoKind == 0) {
        if ((dist - _startDist).abs() > 28) {
          _twoKind = 2;
        } else if ((focal - _startFocal).distance > 12) {
          _twoKind = 1;
        }
      }
      if (_twoKind == 2 && _startDist > 0) {
        final b = _baseRect(_area);
        final scale = (_startScale * dist / _startDist).clamp(1.0, 6.0);
        final n = Offset((_startFocal.dx - _startRect.left) / _startRect.width,
            (_startFocal.dy - _startRect.top) / _startRect.height);
        setState(() {
          _scale = scale < 1.03 ? 1 : scale;
          _setTopLeft(focal - Offset(n.dx * b.width * _scale, n.dy * b.height * _scale), _scale);
        });
      } else if (_twoKind == 1) {
        _scrollRest += focal.dy - _prevFocal.dy;
        while (_scrollRest.abs() >= 26) {
          s.wheel(_scrollRest > 0 ? 1 : -1);
          _scrollRest -= _scrollRest > 0 ? 26 : -26;
        }
      }
      _prevFocal = focal;
    }
  }

  void _endDrag() {
    s.mouse('up', _cur.dx, _cur.dy);
    _dragging = false;
  }

  void _onUp(PointerEvent e) {
    if (s.viewOnly) return _viewUp(e);
    if (!_pts.containsKey(e.pointer)) return;
    _pts.remove(e.pointer);
    if (_multi) {
      if (_pts.length < 2) _twoTimer?.cancel();
      if (_pts.isEmpty) {
        _multi = false;
        if (_twoKind == 0 && DateTime.now().difference(_twoStartAt).inMilliseconds < 450) {
          HapticFeedback.selectionClick();
          _clickAtCursor(button: 'right');
        }
      }
      return;
    }
    _longTimer?.cancel();
    if (e is PointerCancelEvent) {
      if (_dragging) _endDrag();
      return;
    }
    if (_dragging) {
      _endDrag();
      return;
    }
    if (_moved || _longPressed) return;
    if (!_relative) {
      final p = _toRemote(e.localPosition, _viewRect(_area));
      _cur = p;
      s.click(p.dx, p.dy);
      _clicked();
    } else if (_armed) {
      _armed = false;
      _clickAtCursor(times: 2);
    } else {
      _pendingClick = Timer(const Duration(milliseconds: 230), _clickAtCursor);
    }
  }

  void _button(String button, bool down) {
    HapticFeedback.selectionClick();
    s.mouse(down ? 'down' : 'up', _cur.dx, _cur.dy, button: button);
  }

  Widget _mouseButtons() => Row(mainAxisSize: MainAxisSize.min, children: [
        for (final (b, label) in const [('left', 'Left'), ('right', 'Right')])
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6),
            child: Listener(
              onPointerDown: (_) => _button(b, true),
              onPointerUp: (_) => _button(b, false),
              onPointerCancel: (_) => _button(b, false),
              child: Container(
                width: 92,
                height: 52,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: const Color(0xCC111827),
                  borderRadius: BorderRadius.circular(14),
                  border: Border.all(color: Colors.white24),
                ),
                child: Text(label, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
              ),
            ),
          ),
      ]);

  static const _ballSize = 58.0;

  Offset _ballDefault(Size area) => Offset(area.width - _ballSize - 6, area.height * 0.38);

  Offset _clampBall(Offset p, Size area) => Offset(
      p.dx.clamp(0.0, max(0.0, area.width - _ballSize)), p.dy.clamp(0.0, max(0.0, area.height - _ballSize)));

  void _snapBall(Size area) {
    final p = _ball ?? _ballDefault(area);
    final left = p.dx + _ballSize / 2 < area.width / 2;
    _ball = _clampBall(Offset(left ? 6 : area.width - _ballSize - 6, p.dy), area);
  }

  void _closeMenu() => setState(() => _menuOpen = false);

  void _toast(String text) {
    if (!mounted || text.isEmpty) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text), behavior: SnackBarBehavior.floating));
  }

  List<(IconData, String, VoidCallback, Color?)> get _actions => s.viewOnly
      ? [
          (Icons.fit_screen_outlined, 'View', _viewSheet, null),
          (Icons.tune, 'Quality', _qualitySheet, null),
          (Icons.power_settings_new, 'Disconnect', _confirmDisconnect, Colors.red.shade600),
        ]
      : [
          (Icons.keyboard_alt_outlined, 'Keyboard', _toggleKeyboard, null),
          (Icons.fit_screen_outlined, 'View', _viewSheet, null),
          (Icons.mouse_outlined, 'Mouse', _modeSheet, null),
          (Icons.bolt, 'Shortcuts', _shortcutSheet, null),
          (Icons.tune, 'Quality', _qualitySheet, null),
          (Icons.power_settings_new, 'Disconnect', _confirmDisconnect, Colors.red.shade600),
        ];

  List<Widget> _ring(Size area, Offset ball) {
    final actions = _actions;
    const d = 48.0, labelH = 24.0, boxW = 96.0;
    final c = ball + const Offset(_ballSize / 2, _ballSize / 2);
    final radius = min(150.0, (area.height - d - labelH - 16) / 2);
    final cy = c.dy
        .clamp(radius + d / 2 + 6, max(radius + d / 2 + 6, area.height - radius - d / 2 - labelH - 6))
        .toDouble();
    final toward = c.dx > area.width / 2 ? pi : 0.0;
    final out = <Widget>[];
    for (var i = 0; i < actions.length; i++) {
      final a = toward - pi / 2 + pi * i / (actions.length - 1);
      final (icon, label, onTap, color) = actions[i];
      final x = (c.dx + radius * cos(a) - boxW / 2).clamp(2.0, max(2.0, area.width - boxW - 2)).toDouble();
      final y = (cy + radius * sin(a) - d / 2).clamp(2.0, max(2.0, area.height - d - labelH - 2)).toDouble();
      out.add(Positioned(
        left: x,
        top: y,
        width: boxW,
        child: TweenAnimationBuilder<double>(
          tween: Tween(begin: 0, end: 1),
          duration: Duration(milliseconds: 140 + 25 * i),
          curve: Curves.easeOutBack,
          builder: (_, t, child) => Opacity(opacity: t.clamp(0.0, 1.0), child: Transform.scale(scale: t, child: child)),
          child: _RingButton(icon: icon, label: label, color: color, onTap: () {
            _closeMenu();
            onTap();
          }),
        ),
      ));
    }
    return out;
  }

  Widget _ballWidget(Size area) => GestureDetector(
        onTap: () => setState(() => _menuOpen = !_menuOpen),
        onLongPress: () {
          HapticFeedback.mediumImpact();
          setState(() => _menuOpen = true);
        },
        onPanStart: (_) => setState(() {
          _ballDragging = true;
          _menuOpen = false;
        }),
        onPanUpdate: (d) => setState(() => _ball = _clampBall((_ball ?? _ballDefault(area)) + d.delta, area)),
        onPanEnd: (_) => setState(() {
          _ballDragging = false;
          _snapBall(area);
        }),
        child: AnimatedOpacity(
          duration: const Duration(milliseconds: 200),
          opacity: _menuOpen || _ballDragging ? 1 : 0.72,
          child: Container(
            width: _ballSize,
            height: _ballSize,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              gradient: const RadialGradient(colors: [Color(0xFF3B6FF0), brand], radius: 0.9),
              border: Border.all(color: Colors.white.withValues(alpha: 0.85), width: 2),
              boxShadow: const [BoxShadow(color: Color(0x88000000), blurRadius: 12, offset: Offset(0, 4))],
            ),
            child: AnimatedRotation(
              turns: _menuOpen ? 0.125 : 0,
              duration: const Duration(milliseconds: 200),
              child: Icon(_menuOpen ? Icons.add : Icons.apps_rounded, color: Colors.white, size: 28),
            ),
          ),
        ),
      );

  void _viewSheet() {
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (c) => StatefulBuilder(
        builder: (c, setSheet) {
          void update(VoidCallback change) {
            setState(change);
            setSheet(() {});
            _savePrefs();
          }

          final n = s.monitorCount;
          return SafeArea(
            child: ConstrainedBox(
              constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(c).height * 0.8),
              child: ListView(shrinkWrap: true, children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
                  child: Text('View', style: Theme.of(c).textTheme.titleMedium),
                ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: SegmentedButton<bool>(
                    segments: const [
                      ButtonSegment(value: false, icon: Icon(Icons.fit_screen), label: Text('Fit - whole screen')),
                      ButtonSegment(value: true, icon: Icon(Icons.fullscreen), label: Text('Fill - no black bars')),
                    ],
                    selected: {_fill},
                    onSelectionChanged: (v) => update(() {
                      _fill = v.first;
                      _scale = 1;
                      _pan = Offset.zero;
                    }),
                  ),
                ),
                SwitchListTile(
                  value: _landscape,
                  title: const Text('Keep landscape'),
                  subtitle: const Text('PC screens are wide; turn off to rotate freely'),
                  onChanged: (v) => update(() {
                    _landscape = v;
                    _applyOrientation();
                  }),
                ),
                if (n > 1) ...[
                  const Divider(),
                  for (var i = 1; i <= n; i++)
                    ListTile(
                      leading: const Icon(Icons.monitor),
                      title: Text('Screen $i'),
                      trailing: s.monitor == i ? const Icon(Icons.check, color: brand) : null,
                      onTap: () {
                        Navigator.pop(c);
                        s.setMonitor(i);
                      },
                    ),
                  ListTile(
                    leading: const Icon(Icons.view_column_outlined),
                    title: const Text('All screens'),
                    trailing: s.monitor == 0 ? const Icon(Icons.check, color: brand) : null,
                    onTap: () {
                      Navigator.pop(c);
                      s.setMonitor(0);
                    },
                  ),
                ],
                const SizedBox(height: 12),
              ]),
            ),
          );
        },
      ),
    );
  }

  void _applyOrientation() {
    SystemChrome.setPreferredOrientations(
        _landscape ? const [DeviceOrientation.landscapeLeft, DeviceOrientation.landscapeRight] : const []);
  }

  void _modeSheet() {
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (c) => StatefulBuilder(
        builder: (c, setSheet) {
          void pick(MouseMode m) {
            setState(() => _mode = m);
            setSheet(() {});
            _savePrefs();
          }

          return SafeArea(
            child: ListView(shrinkWrap: true, children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
                child: Text('How to control', style: Theme.of(c).textTheme.titleMedium),
              ),
              for (final (m, title, help) in const [
                (MouseMode.touchpad, 'Touchpad', 'Slide to move the pointer, tap to click. Hold, then slide to select or drag.'),
                (MouseMode.buttons, 'Touchpad + buttons', 'Like Touchpad, with Left / Right buttons. Hold Left and slide to drag or select.'),
                (MouseMode.touch, 'Touch', 'Tap exactly where you touch; hold or slide to select / drag - like a touch screen.'),
              ])
                ListTile(
                  leading: Icon(m == _mode ? Icons.radio_button_checked : Icons.radio_button_off, color: brand),
                  title: Text(title),
                  subtitle: Text(help),
                  onTap: () => pick(m),
                ),
              SwitchListTile(
                value: _autoKeyboard,
                title: const Text('Automatic keyboard'),
                subtitle: const Text('Opens when you tap into a text box on the PC, closes when you leave it'),
                onChanged: (v) {
                  setState(() => _autoKeyboard = v);
                  setSheet(() {});
                  _savePrefs();
                },
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 8, 20, 0),
                child: Text('Pointer speed', style: Theme.of(c).textTheme.labelLarge),
              ),
              Slider(
                value: _pointerSpeed,
                min: 0.4,
                max: 2.5,
                divisions: 21,
                label: '${_pointerSpeed.toStringAsFixed(1)}x',
                onChanged: (v) {
                  setState(() => _pointerSpeed = v);
                  setSheet(() {});
                },
                onChangeEnd: (_) => _savePrefs(),
              ),
              const Padding(
                padding: EdgeInsets.fromLTRB(20, 0, 20, 16),
                child: Text('Everywhere: two-finger tap or hold = right click, two fingers slide = scroll, pinch = zoom.'),
              ),
            ]),
          );
        },
      ),
    );
  }

  Future<void> _loadPrefs() async {
    final p = await SharedPreferences.getInstance();
    final m = p.getString('mouseMode');
    setState(() {
      _mode = MouseMode.values.where((x) => x.name == m).firstOrNull ?? MouseMode.touchpad;
      _pointerSpeed = p.getDouble('pointerSpeed') ?? 1.0;
      _fill = p.getBool('fillScreen') ?? false;
      _landscape = p.getBool('landscape') ?? true;
      _autoKeyboard = p.getBool('autoKeyboard') ?? true;
      _pill = Offset(p.getDouble('pillRight') ?? 20, p.getDouble('pillBottom') ?? 8);
      _pillMini = p.getBool('pillMini') ?? false;
    });
    final saved = qualities.where((q) => q.name == p.getString('quality')).firstOrNull;
    if (saved != null && saved != _quality) {
      setState(() => _quality = saved);
      s.setQuality(saved);
    }
    _applyOrientation();
    if (p.getBool('controlHintShown') != true) {
      await p.setBool('controlHintShown', true);
      _toast('Slide to move the pointer, tap to click, hold and slide to select, two fingers = right click / scroll.');
    }
  }

  Future<void> _savePrefs() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('mouseMode', _mode.name);
    await p.setDouble('pointerSpeed', _pointerSpeed);
    await p.setBool('fillScreen', _fill);
    await p.setBool('landscape', _landscape);
    await p.setBool('autoKeyboard', _autoKeyboard);
    await p.setString('quality', _quality.name);
  }

  void _ctrlAltDel() {
    s.ctrlAltDel();
    _toast(s.adminAccess
        ? 'Ctrl+Alt+Del sent - type the Windows password with the keyboard'
        : 'Ctrl+Alt+Del sent (needs the admin service on that PC)');
  }

  Future<void> _sheet(String title, List<Widget> children) => showModalBottomSheet<void>(
        context: context,
        showDragHandle: true,
        isScrollControlled: true,
        builder: (c) => SafeArea(
          child: ConstrainedBox(
            constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(c).height * 0.7),
            child: ListView(shrinkWrap: true, children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
                child: Text(title, style: Theme.of(c).textTheme.titleMedium),
              ),
              ...children,
            ]),
          ),
        ),
      );

  void _shortcutSheet() {
    _sheet('Shortcuts', [
      ListTile(
        leading: const Icon(Icons.lock_open),
        title: const Text('Ctrl+Alt+Del'),
        subtitle: const Text('Unlock / security screen'),
        onTap: () {
          Navigator.pop(context);
          _ctrlAltDel();
        },
      ),
      for (final (label, keys) in shortcuts)
        ListTile(
          leading: const Icon(Icons.keyboard_command_key),
          title: Text(label),
          onTap: () {
            Navigator.pop(context);
            s.combo(keys);
          },
        ),
      ListTile(
        leading: const Icon(Icons.lock_outline),
        title: const Text('Lock the PC (Win+L)'),
        onTap: () async {
          Navigator.pop(context);
          final ok = await showDialog<bool>(
            context: context,
            builder: (c) => AlertDialog(
              title: const Text('Lock the PC?'),
              content: Text(s.adminAccess
                  ? 'Unlock it again with Ctrl+Alt+Del and the Windows password.'
                  : 'Without the admin service on that PC you cannot unlock it from here.'),
              actions: [
                TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
                FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Lock')),
              ],
            ),
          );
          if (ok == true) s.lock();
        },
      ),
    ]);
  }

  void _qualitySheet() {
    _sheet('Picture quality', [
      for (final q in qualities)
        ListTile(
          leading: const Icon(Icons.high_quality_outlined),
          title: Text(q.name),
          trailing: q == _quality ? const Icon(Icons.check, color: brand) : null,
          onTap: () {
            Navigator.pop(context);
            setState(() => _quality = q);
            s.setQuality(q);
            _savePrefs();
          },
        ),
    ]);
  }

  Future<void> _confirmDisconnect() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Disconnect?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Stay')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Disconnect')),
        ],
      ),
    );
    if (ok == true) _disconnect();
  }

  Widget _keyStrip() {
    if (!_keysMore) {
      return Material(
        color: const Color(0xF0111827),
        shape: const StadiumBorder(),
        child: Row(mainAxisSize: MainAxisSize.min, children: _pillMini
            ? [
                _IconKey(Icons.keyboard_outlined, 'Keyboard - hold for all buttons', _flipPhoneKeyboard,
                    onHold: () => _setPillMini(false)),
              ]
            : [
                _IconKey(Icons.keyboard_command_key, 'More keys: Ctrl, Alt, Esc, Tab, arrows, F1-F12',
                    () => setState(() => _keysMore = true)),
                _IconKey(Icons.content_paste, 'Paste', _paste),
                _IconKey(Icons.keyboard_outlined, 'Show keyboard', () => _setKeyboard(true)),
                _IconKey(Icons.keyboard_hide_outlined, 'Hide keyboard', _toggleKeyboard,
                    onHold: () => _setPillMini(true)),
              ]),
      );
    }
    return Material(
      color: const Color(0xF0111827),
      child: SizedBox(
        height: 48,
        child: ListView(scrollDirection: Axis.horizontal, padding: const EdgeInsets.symmetric(horizontal: 4), children: [
          _IconKey(Icons.chevron_right, 'Fewer keys', () => setState(() {
                _keysMore = false;
                _mods.clear();
              })),
          for (final m in const [('ctrl', 'Ctrl'), ('alt', 'Alt'), ('shift', 'Shift'), ('cmd', 'Win')])
            _ToolButton(null, m.$2, () => setState(() => _mods.contains(m.$1) ? _mods.remove(m.$1) : _mods.add(m.$1)),
                active: _mods.contains(m.$1)),
          _ToolButton(Icons.content_copy, 'Copy', () => s.combo(['ctrl', 'c'])),
          _ToolButton(Icons.content_paste, 'Paste', _paste),
          _ToolButton(null, 'Esc', () => _sendKey('Escape')),
          _ToolButton(null, 'Tab', () => _sendKey('Tab')),
          _ToolButton(null, 'Enter', () => _sendKey('Return')),
          _ToolButton(null, 'Del', () => _sendKey('Delete')),
          _ToolButton(Icons.keyboard_arrow_left, '', () => _sendKey('Left')),
          _ToolButton(Icons.keyboard_arrow_up, '', () => _sendKey('Up')),
          _ToolButton(Icons.keyboard_arrow_down, '', () => _sendKey('Down')),
          _ToolButton(Icons.keyboard_arrow_right, '', () => _sendKey('Right')),
          _ToolButton(null, 'Home', () => _sendKey('Home')),
          _ToolButton(null, 'End', () => _sendKey('End')),
          for (var i = 1; i <= 12; i++) _ToolButton(null, 'F$i', () => _sendKey('F$i')),
          _ToolButton(Icons.keyboard_hide_outlined, '', _toggleKeyboard),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        if (_menuOpen) {
          _closeMenu();
        } else {
          _confirmDisconnect();
        }
      },
      child: Scaffold(
        backgroundColor: Colors.black,
        body: LayoutBuilder(builder: (context, c) {
          final area = Size(c.maxWidth, c.maxHeight);
          final ball = _clampBall(_ball ?? _ballDefault(area), area);
          final top = MediaQuery.paddingOf(context).top;
          _area = area;
          final rect = _viewRect(area);
          final cursor = _mode != MouseMode.touch && !s.viewOnly
              ? _cur
              : (s.cursorVisible && s.cursorX >= 0 ? Offset(s.cursorX, s.cursorY) : null);
          final unit = s.width > 0 ? max(rect.width / s.width, 0.55) : 1.0;
          return Stack(children: [
            Positioned.fill(
              child: Listener(
                behavior: HitTestBehavior.opaque,
                onPointerDown: _onDown,
                onPointerMove: _onMove,
                onPointerUp: _onUp,
                onPointerCancel: _onUp,
                child: CustomPaint(painter: _ScreenPainter(s.image, rect, cursor, unit), size: area),
              ),
            ),
            if (s.width <= 0) const Center(child: CircularProgressIndicator()),
            Positioned(
              left: 0,
              top: 0,
              width: 1,
              height: 1,
              child: Opacity(
                opacity: 0,
                child: TextField(
                  controller: _typing,
                  focusNode: _typingFocus,
                  autocorrect: false,
                  enableSuggestions: false,
                  keyboardType: TextInputType.visiblePassword,
                  textInputAction: TextInputAction.newline,
                  maxLines: 1,
                  onChanged: _onTyped,
                  onEditingComplete: () {},
                  onSubmitted: (_) => _sendKey('Return'),
                ),
              ),
            ),
            if (s.viewOnly && s.warning.isEmpty && s.inputAllowed)
              Positioned(
                left: 8,
                top: top + 8,
                child: const IgnorePointer(
                  child: Material(
                    color: Color(0xCC111827),
                    shape: StadiumBorder(),
                    child: Padding(
                      padding: EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                      child: Row(mainAxisSize: MainAxisSize.min, children: [
                        Icon(Icons.visibility_outlined, color: Colors.white, size: 16),
                        SizedBox(width: 6),
                        Text('View only', style: TextStyle(color: Colors.white, fontSize: 13)),
                      ]),
                    ),
                  ),
                ),
              ),
            if (s.warning.isNotEmpty || !s.inputAllowed)
              Positioned(
                left: 8,
                right: 8,
                top: top + 8,
                child: _Banner(!s.inputAllowed ? 'View only: remote control is switched off on that PC.' : s.warning),
              ),
            if (_scale > 1.001)
              Positioned(
                top: top + 8,
                left: 0,
                right: 0,
                child: Center(
                  child: ActionChip(
                    avatar: const Icon(Icons.zoom_out_map, size: 18),
                    label: Text('Zoom ${_scale.toStringAsFixed(1)}x - tap to fit'),
                    onPressed: _fit,
                  ),
                ),
              ),
            if (_mode == MouseMode.buttons && !s.viewOnly)
              Positioned(left: 0, right: 0, bottom: _keysOpen ? 60 : 14, child: Center(child: _mouseButtons())),
            if (_keysOpen && _keysMore) Positioned(left: 0, right: 0, bottom: 0, child: _keyStrip()),
            if (_keysOpen && !_keysMore)
              Positioned(
                right: _pill.dx.clamp(0.0, max(0.0, area.width - _pillWidth)),
                bottom: _pill.dy.clamp(0.0, max(0.0, area.height - 60 - MediaQuery.paddingOf(context).top)),
                child: GestureDetector(
                  onPanUpdate: (d) => setState(() => _pill = Offset(
                      (_pill.dx.clamp(0.0, max(0.0, area.width - _pillWidth)) - d.delta.dx)
                          .clamp(0.0, max(0.0, area.width - _pillWidth)),
                      (_pill.dy.clamp(0.0, max(0.0, area.height - 60)) - d.delta.dy)
                          .clamp(0.0, max(0.0, area.height - 60 - MediaQuery.paddingOf(context).top)))),
                  onPanEnd: (_) => _savePill(),
                  child: _keyStrip(),
                ),
              ),
            if (_reconnecting)
              const Positioned.fill(
                child: ColoredBox(
                  color: Color(0x99000000),
                  child: Center(
                    child: Column(mainAxisSize: MainAxisSize.min, children: [
                      CircularProgressIndicator(),
                      SizedBox(height: 12),
                      Text('Connection lost - reconnecting...', style: TextStyle(color: Colors.white)),
                    ]),
                  ),
                ),
              ),
            if (_menuOpen)
              Positioned.fill(
                child: GestureDetector(
                  onTap: _closeMenu,
                  child: const ColoredBox(color: Color(0x73000000)),
                ),
              ),
            if (_menuOpen) ..._ring(area, ball),
            AnimatedPositioned(
              duration: _ballDragging ? Duration.zero : const Duration(milliseconds: 220),
              curve: Curves.easeOut,
              left: ball.dx,
              top: ball.dy,
              child: _ballWidget(area),
            ),
          ]);
        }),
      ),
    );
  }
}

class _RingButton extends StatelessWidget {
  final IconData icon;
  final String label;
  final Color? color;
  final VoidCallback onTap;
  const _RingButton({required this.icon, required this.label, required this.onTap, this.color});
  @override
  Widget build(BuildContext context) => GestureDetector(
        onTap: onTap,
        behavior: HitTestBehavior.opaque,
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Container(
            width: 48,
            height: 48,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: color ?? Colors.white,
              boxShadow: const [BoxShadow(color: Color(0x66000000), blurRadius: 8, offset: Offset(0, 3))],
            ),
            child: Icon(icon, color: color == null ? brand : Colors.white, size: 23),
          ),
          const SizedBox(height: 4),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
            decoration: BoxDecoration(color: const Color(0xCC101218), borderRadius: BorderRadius.circular(6)),
            child: Text(label,
                maxLines: 1,
                softWrap: false,
                style: const TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.w600)),
          ),
        ]),
      );
}

class _ScreenPainter extends CustomPainter {
  final ui.Image? image;
  final Rect rect;
  final Offset? cursor;
  final double unit;
  _ScreenPainter(this.image, this.rect, this.cursor, this.unit);

  @override
  void paint(Canvas canvas, Size size) {
    final img = image;
    if (img != null) {
      canvas.drawImageRect(img, Rect.fromLTWH(0, 0, img.width.toDouble(), img.height.toDouble()), rect,
          Paint()..filterQuality = FilterQuality.low);
    } else {
      canvas.drawRect(rect, Paint()..color = const Color(0xFF111111));
    }
    final c = cursor;
    if (c == null) return;
    final p = Offset(rect.left + c.dx * rect.width, rect.top + c.dy * rect.height);
    final k = unit;
    final arrow = Path()
      ..moveTo(p.dx, p.dy)
      ..lineTo(p.dx, p.dy + 17 * k)
      ..lineTo(p.dx + 4 * k, p.dy + 13 * k)
      ..lineTo(p.dx + 8 * k, p.dy + 20 * k)
      ..lineTo(p.dx + 11 * k, p.dy + 19 * k)
      ..lineTo(p.dx + 7 * k, p.dy + 12 * k)
      ..lineTo(p.dx + 12 * k, p.dy + 12 * k)
      ..close();
    canvas.drawShadow(arrow, Colors.black, 2, false);
    canvas.drawPath(arrow, Paint()..color = Colors.white);
    canvas.drawPath(
        arrow,
        Paint()
          ..color = Colors.black
          ..style = PaintingStyle.stroke
          ..strokeWidth = max(0.8, k));
  }

  @override
  bool shouldRepaint(_ScreenPainter old) => true;
}

class _IconKey extends StatelessWidget {
  final IconData icon;
  final String tip;
  final VoidCallback onTap;
  final VoidCallback? onHold;
  const _IconKey(this.icon, this.tip, this.onTap, {this.onHold});
  @override
  Widget build(BuildContext context) => Tooltip(
        message: tip,
        triggerMode: onHold != null ? TooltipTriggerMode.manual : TooltipTriggerMode.longPress,
        child: InkResponse(
          onTap: onTap,
          onLongPress: onHold == null
              ? null
              : () {
                  HapticFeedback.mediumImpact();
                  onHold!();
                },
          radius: 24,
          child: Padding(padding: const EdgeInsets.all(13), child: Icon(icon, size: 22, color: Colors.white)),
        ),
      );
}

class _ToolButton extends StatelessWidget {
  final IconData? icon;
  final String label;
  final VoidCallback onTap;
  final bool active;
  const _ToolButton(this.icon, this.label, this.onTap, {this.active = false});
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: 2, vertical: 8),
        child: TextButton(
          style: TextButton.styleFrom(
            foregroundColor: Colors.white,
            backgroundColor: active ? brand : null,
            padding: const EdgeInsets.symmetric(horizontal: 10),
          ),
          onPressed: onTap,
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            if (icon != null) Icon(icon, size: 20),
            if (icon != null && label.isNotEmpty) const SizedBox(width: 4),
            if (label.isNotEmpty) Text(label),
          ]),
        ),
      );
}

class _Banner extends StatelessWidget {
  final String text;
  const _Banner(this.text);
  @override
  Widget build(BuildContext context) => Material(
        color: const Color(0xEEB45309),
        borderRadius: BorderRadius.circular(8),
        child: Padding(padding: const EdgeInsets.all(10), child: Text(text, style: const TextStyle(color: Colors.white))),
      );
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  int _tab = 0;

  @override
  Widget build(BuildContext context) => Scaffold(
        body: IndexedStack(index: _tab, children: const [ConnectPage(), SharePage()]),
        bottomNavigationBar: NavigationBar(
          selectedIndex: _tab,
          onDestinationSelected: (i) => setState(() => _tab = i),
          destinations: const [
            NavigationDestination(icon: Icon(Icons.desktop_windows_outlined), selectedIcon: Icon(Icons.desktop_windows), label: 'Connect'),
            NavigationDestination(icon: Icon(Icons.phone_android_outlined), selectedIcon: Icon(Icons.phone_android), label: 'Share this phone'),
          ],
        ),
      );
}

class SharePage extends StatefulWidget {
  const SharePage({super.key});
  @override
  State<SharePage> createState() => _SharePageState();
}

class _SharePageState extends State<SharePage> with WidgetsBindingObserver {
  final _vault = const FlutterSecureStorage();
  String _id = '', _password = '', _name = 'This phone', _message = '';
  List<String> _addresses = [];
  Set<String> _tailscaleAddrs = {};
  Timer? _addrPoll;
  bool _showPassword = false, _control = false, _starting = false, _onlyOne = false;
  String? _onlyIp;
  PhoneHost? _host;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load();
    _addrPoll = Timer.periodic(const Duration(seconds: 10), (_) => _loadAddresses());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _addrPoll?.cancel();
    super.dispose();
  }

  Future<void> _loadAddresses() async {
    int rank(String nic) => nic.startsWith('wlan') ? 0 : nic.startsWith('eth') ? 1
        : (nic.startsWith('rmnet') || nic.startsWith('ccmni')) ? 2 : nic.startsWith('tun') ? 4 : 3;
    final found = <(int, String)>[];
    final ts = <String>{};
    try {
      for (final nic in await NetworkInterface.list(type: InternetAddressType.IPv4)) {
        for (final a in nic.addresses) {
          if (a.isLoopback || a.address.startsWith('169.254.')) continue;
          found.add((rank(nic.name), a.address));
          if (nic.name.startsWith('tun') && isTailscaleIp(a.address)) ts.add('${a.address}:$defaultPort');
        }
      }
    } catch (_) {}
    found.sort((a, b) => a.$1 - b.$1);
    final list = [for (final (_, ip) in found) '$ip:$defaultPort'];
    if (mounted && list.join() != _addresses.join()) {
      setState(() {
        _addresses = list;
        _tailscaleAddrs = ts;
      });
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _checkControl();
      _loadAddresses();
    }
  }

  Future<void> _load() async {
    final p = await SharedPreferences.getInstance();
    var id = p.getString('shareId') ?? '';
    var pw = await _vault.read(key: 'sharePassword') ?? '';
    if (id.isEmpty || pw.length < 8) {
      (id, pw) = newPhoneIdentity();
      await p.setString('shareId', id);
      await _vault.write(key: 'sharePassword', value: pw);
    }
    final name = await PhoneBridge.deviceName();
    if (!mounted) return;
    setState(() {
      _id = id;
      _password = pw;
      _name = name;
      _onlyOne = p.getBool('shareOnlyOne') ?? false;
      _onlyIp = p.getString('shareIp');
    });
    _checkControl();
    _loadAddresses();
  }

  Future<void> _checkControl() async {
    final on = await PhoneBridge.controlEnabled();
    if (mounted) setState(() => _control = on);
  }

  Future<void> _changePassword() async {
    if (_host?.running ?? false) {
      setState(() => _message = 'Stop sharing first to change the password');
      return;
    }
    final field = TextEditingController(text: _password);
    var shown = false;
    String? error;
    final pw = await showDialog<String>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, setD) => AlertDialog(
          title: const Text('Password for this phone'),
          content: TextField(
            controller: field,
            autofocus: true,
            obscureText: !shown,
            decoration: InputDecoration(
              labelText: 'New password',
              helperText: 'At least 8 characters',
              errorText: error,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(shown ? Icons.visibility_off : Icons.visibility),
                onPressed: () => setD(() => shown = !shown),
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => setD(() {
                field.text = newPhoneIdentity().$2;
                shown = true;
                error = null;
              }),
              child: const Text('Random'),
            ),
            TextButton(onPressed: () => Navigator.pop(c), child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                final v = field.text.trim();
                if (v.length < 8 || v.length > 64) {
                  setD(() => error = 'Use 8 to 64 characters');
                  return;
                }
                Navigator.pop(c, v);
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
    if (pw == null || !mounted) return;
    await _vault.write(key: 'sharePassword', value: pw);
    setState(() {
      _password = pw;
      _message = '';
    });
  }

  Future<void> _start() async {
    setState(() {
      _starting = true;
      _message = '';
    });
    try {
      if (!await PhoneBridge.startCapture()) {
        setState(() => _message = 'Sharing needs "Start now" on the Android screen');
        return;
      }
      final p = await SharedPreferences.getInstance();
      final servers = splitServers(p.getString('server') ?? defaultServer);
      final ips = [for (final a in _addresses) a.split(':').first];
      final only = _onlyOne ? (ips.contains(_onlyIp) ? _onlyIp : (ips.isNotEmpty ? ips.first : null)) : null;
      final host = PhoneHost(id: _id, password: _password, name: _name, servers: servers, onlyAddress: only);
      host.onStopped = (reason) {
        if (mounted) setState(() => _message = reason);
      };
      host.status.addListener(() => mounted ? setState(() {}) : null);
      host.connected.addListener(() => mounted ? setState(() {}) : null);
      _host = host;
      await host.start();
      setState(() {});
    } finally {
      if (mounted) setState(() => _starting = false);
    }
  }

  Future<void> _stop() async {
    await _host?.stop();
    setState(() => _host = null);
  }

  List<String> get _ips => [for (final a in _addresses) a.split(':').first];

  List<String> get _shownAddresses {
    if (!_onlyOne) return _addresses;
    final ip = _ips.contains(_onlyIp) ? _onlyIp : (_ips.isNotEmpty ? _ips.first : null);
    return [for (final a in _addresses) if (a.split(':').first == ip) a];
  }

  Future<void> _setOnlyOne(bool only, String ip) async {
    setState(() {
      _onlyOne = only;
      _onlyIp = ip;
    });
    final p = await SharedPreferences.getInstance();
    await p.setBool('shareOnlyOne', only);
    await p.setString('shareIp', ip);
  }

  void _copy(String text, String what) {
    Clipboard.setData(ClipboardData(text: text));
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$what copied'), behavior: SnackBarBehavior.floating));
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    final sharing = _host?.running ?? false;
    final connected = _host?.connected.value ?? false;
    return Scaffold(
      appBar: AppBar(
        title: const Row(children: [_Logo(size: 28), SizedBox(width: 10), Text('AnyKir')]),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: Chip(label: const Text('v$appVersion'), visualDensity: VisualDensity.compact, side: BorderSide.none,
                backgroundColor: t.colorScheme.secondaryContainer),
          ),
        ],
      ),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.all(16), children: [
          Text('Share this phone', style: t.textTheme.titleLarge),
          const SizedBox(height: 4),
          Text('Control this phone from a PC with AnyKir: pick it under "PCs online" on the PC, or type the address below.',
              style: t.textTheme.bodyMedium?.copyWith(color: t.colorScheme.onSurfaceVariant)),
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(_name, style: t.textTheme.titleMedium),
                const SizedBox(height: 10),
                Text('Address', style: t.textTheme.labelMedium?.copyWith(color: t.colorScheme.onSurfaceVariant)),
                if (_addresses.isEmpty)
                  const Padding(padding: EdgeInsets.symmetric(vertical: 10), child: Text('No network'))
                else
                  for (final a in _shownAddresses)
                    Row(children: [
                      Expanded(
                        child: Text.rich(TextSpan(children: [
                          TextSpan(text: a, style: t.textTheme.titleLarge?.copyWith(fontFamily: 'monospace')),
                          if (_tailscaleAddrs.contains(a))
                            TextSpan(text: '  Tailscale', style: t.textTheme.labelMedium?.copyWith(color: t.colorScheme.primary)),
                        ])),
                      ),
                      IconButton(onPressed: () => _copy(a, 'Address'), icon: const Icon(Icons.copy), tooltip: 'Copy'),
                    ]),
                if (_addresses.length > 1) ...[
                  const SizedBox(height: 4),
                  Wrap(spacing: 8, runSpacing: 6, crossAxisAlignment: WrapCrossAlignment.center, children: [
                    Text('Share on', style: t.textTheme.labelMedium?.copyWith(color: t.colorScheme.onSurfaceVariant)),
                    SegmentedButton<bool>(
                      segments: const [
                        ButtonSegment(value: false, label: Text('All addresses')),
                        ButtonSegment(value: true, label: Text('Only one')),
                      ],
                      selected: {_onlyOne},
                      showSelectedIcon: false,
                      onSelectionChanged: sharing ? null : (v) => _setOnlyOne(v.first, _onlyIp ?? _ips.first),
                    ),
                    if (_onlyOne)
                      DropdownButton<String>(
                        value: _ips.contains(_onlyIp) ? _onlyIp : _ips.first,
                        items: [
                          for (final ip in _ips)
                            DropdownMenuItem(value: ip, child: Text(ip + (isTailscaleIp(ip) ? '  (Tailscale)' : ''))),
                        ],
                        onChanged: sharing ? null : (ip) => _setOnlyOne(true, ip ?? _ips.first),
                      ),
                  ]),
                  if (sharing)
                    Text('Stop sharing to change this.', style: t.textTheme.bodySmall),
                ],
                const SizedBox(height: 6),
                Text('Password', style: t.textTheme.labelMedium?.copyWith(color: t.colorScheme.onSurfaceVariant)),
                Row(children: [
                  Expanded(
                    child: Text(_showPassword ? _password : '•' * _password.length,
                        style: t.textTheme.titleLarge?.copyWith(fontFamily: 'monospace')),
                  ),
                  IconButton(
                    onPressed: () => setState(() => _showPassword = !_showPassword),
                    icon: Icon(_showPassword ? Icons.visibility_off : Icons.visibility),
                    tooltip: _showPassword ? 'Hide' : 'Show',
                  ),
                  IconButton(onPressed: () => _copy(_password, 'Password'), icon: const Icon(Icons.copy), tooltip: 'Copy'),
                  IconButton(onPressed: _changePassword, icon: const Icon(Icons.edit), tooltip: 'Change'),
                ]),
              ]),
            ),
          ),
          Card(
            color: _control ? Colors.green.withValues(alpha: 0.12) : t.colorScheme.surfaceContainerHighest,
            child: ListTile(
              leading: Icon(_control ? Icons.touch_app : Icons.touch_app_outlined,
                  color: _control ? Colors.green.shade700 : t.colorScheme.outline),
              title: Text(_control ? 'Remote control: On' : 'Remote control: Off'),
              subtitle: Text(_control
                  ? 'The PC can tap, swipe and type'
                  : 'The PC can only watch. Turn on "AnyKir remote control" in Accessibility.'),
              trailing: TextButton(onPressed: PhoneBridge.openControlSettings, child: Text(_control ? 'Settings' : 'Turn on')),
            ),
          ),
          const SizedBox(height: 8),
          if (!sharing)
            FilledButton.icon(
              onPressed: _starting || _id.isEmpty ? null : _start,
              icon: _starting
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.screen_share),
              label: const Text('Start sharing'),
            )
          else
            FilledButton.tonalIcon(
              onPressed: _stop,
              icon: const Icon(Icons.stop_screen_share),
              label: const Text('Stop sharing'),
            ),
          const SizedBox(height: 12),
          if (sharing)
            Card(
              child: ListTile(
                leading: Icon(connected ? Icons.link : Icons.wifi_tethering, color: connected ? Colors.green.shade700 : null),
                title: Text(_host!.status.value),
                trailing: connected
                    ? TextButton(onPressed: () => _host?.disconnectViewer(), child: const Text('Disconnect'))
                    : null,
              ),
            ),
          if (_message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(_message, style: TextStyle(color: t.colorScheme.error)),
            ),
        ]),
      ),
    );
  }
}

class _Logo extends StatelessWidget {
  final double size;
  const _Logo({required this.size});
  @override
  Widget build(BuildContext context) => CustomPaint(size: Size.square(size), painter: _LogoPainter());
}

class _LogoPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final k = size.width / 256;
    Rect r(double a, double b, double c, double d) => Rect.fromLTRB(a * k, b * k, c * k, d * k);
    canvas.drawRRect(RRect.fromRectAndRadius(r(8, 8, 248, 248), Radius.circular(52 * k)), Paint()..color = brand);
    canvas.drawRRect(RRect.fromRectAndRadius(r(39, 49, 159, 133), Radius.circular(14 * k)),
        Paint()..color = const Color(0x6EFFFFFF));
    canvas.drawRRect(RRect.fromRectAndRadius(r(61, 87, 217, 207), Radius.circular(24 * k)), Paint()..color = brand);
    canvas.drawRRect(RRect.fromRectAndRadius(r(71, 97, 207, 197), Radius.circular(14 * k)), Paint()..color = Colors.white);
    const f = 52.0 / 128, cx = 139.0, cy = 147.0;
    double x(double v) => (cx + (v - 128) * f) * k;
    double y(double v) => (cy + (v - 128) * f) * k;
    final blue = Paint()..color = brand;
    canvas.drawRect(Rect.fromLTRB(x(58), y(64), x(94), y(192)), blue);
    canvas.drawPath(
        Path()
          ..moveTo(x(110), y(128))
          ..lineTo(x(162), y(64))
          ..lineTo(x(198), y(64))
          ..lineTo(x(146), y(128))
          ..lineTo(x(198), y(192))
          ..lineTo(x(162), y(192))
          ..close(),
        blue);
  }

  @override
  bool shouldRepaint(_) => false;
}
