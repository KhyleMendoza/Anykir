import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'protocol.dart';
import 'version.dart';

const _share = MethodChannel('anykir/share');
const _input = MethodChannel('anykir/input');

class PhoneBridge {
  static Future<bool> startCapture() async => await _share.invokeMethod<bool>('start') ?? false;
  static Future<void> stopCapture() => _share.invokeMethod('stop');
  static Future<bool> capturing() async => await _share.invokeMethod<bool>('capturing') ?? false;
  static Future<String> deviceName() async => await _share.invokeMethod<String>('deviceName') ?? 'Android phone';

  static Future<Map?> frame(int quality, bool force) =>
      _share.invokeMethod<Map>('frame', {'quality': quality, 'force': force});

  static Future<bool> controlEnabled() async => await _input.invokeMethod<bool>('enabled') ?? false;
  static Future<void> openControlSettings() => _input.invokeMethod('settings');
  static Future<void> gesture(List<double> points, int ms) =>
      _input.invokeMethod('gesture', {'points': points, 'ms': ms});
  static Future<void> global(String name) => _input.invokeMethod('global', {'name': name});
  static Future<void> type(String text) => _input.invokeMethod('type', {'text': text});
  static Future<void> key(String name) => _input.invokeMethod('key', {'name': name});
}

class _HostSession {
  final SecureChannel ch;
  final PhoneHost host;
  bool closed = false, flowOn = false;
  int sent = 0, acked = 0, quality = 60, fps = 15;
  bool force = true;
  int width = 0, height = 0;
  double? downX, downY;
  int downAt = 0;
  final List<double> path = [];
  double lastX = 0.5, lastY = 0.5;
  final StringBuffer typed = StringBuffer();
  Timer? typeTimer;

  _HostSession(this.ch, this.host);

  Future<void> run() async {
    final control = await PhoneBridge.controlEnabled();
    await ch.sendJson({
      't': 'hello', 'host': host.name, 'os': 'Android', 'input': control, 'ver': appVersion,
      'admin': false, 'uac': false, 'files': false, 'clipboard': false, 'upd': false, 'rel': null,
    });
    unawaited(_frames());
    try {
      while (!closed) {
        final (type, body) = await ch.recv();
        if (type == tJson) _onMessage(jsonDecode(utf8.decode(body)) as Map<String, dynamic>);
      }
    } catch (_) {}
    close();
  }

  void close() {
    if (closed) return;
    closed = true;
    typeTimer?.cancel();
    ch.close();
    host._ended(this);
  }

  Future<void> _frames() async {
    while (!closed) {
      final started = DateTime.now();
      if (flowOn) {
        while (!closed && sent - acked >= 6) {
          await Future<void>.delayed(const Duration(milliseconds: 10));
        }
      }
      try {
        final f = await PhoneBridge.frame(quality, force);
        if (f != null && !closed) {
          force = false;
          final w = f['w'] as int, h = f['h'] as int;
          if (w != width || h != height) {
            width = w;
            height = h;
            await ch.sendJson({'t': 'screen', 'w': w, 'h': h, 'count': 1, 'mon': 1, 'cap': 'android'});
          }
          final jpeg = f['jpeg'] as Uint8List;
          final head = ByteData(8)
            ..setUint16(0, 0)
            ..setUint16(2, 0)
            ..setUint16(4, w)
            ..setUint16(6, h);
          await ch.send(tFrame, [...head.buffer.asUint8List(), ...jpeg]);
          sent++;
        } else if (f == null && !await PhoneBridge.capturing()) {
          await ch.sendJson({'t': 'capture_status', 'ok': false, 'err': 'Screen sharing stopped on the phone'});
          close();
          return;
        }
      } catch (_) {}
      final spent = DateTime.now().difference(started).inMilliseconds;
      final wait = max(5, (1000 / fps.clamp(1, 30)).round() - spent);
      await Future<void>.delayed(Duration(milliseconds: wait));
    }
  }

  void _onMessage(Map<String, dynamic> m) {
    switch (m['t']) {
      case 'ack':
        acked = max(acked, (m['n'] as num?)?.toInt() ?? 0);
      case 'caps':
        flowOn = m['ack'] == 1 || m['ack'] == true;
      case 'cfg':
        quality = ((m['quality'] as num?) ?? quality).toInt().clamp(20, 95);
        fps = ((m['fps'] as num?) ?? fps).toInt().clamp(1, 30);
        force = true;
      case 'refresh':
        force = true;
      case 'mouse':
        _mouse(m);
      case 'wheel':
        _wheel(m);
      case 'key':
        if (m['a'] == 'down') _key('${m['k'] ?? ''}', '${m['c'] ?? ''}');
      case 'combo':
        _combo([for (final k in (m['keys'] as List? ?? const [])) '$k'.toLowerCase()]);
      case 'lock':
        PhoneBridge.global('lock');
      case 'cad':
        PhoneBridge.global('lock');
    }
  }

  void _mouse(Map<String, dynamic> m) {
    final x = ((m['x'] as num?) ?? 0).toDouble(), y = ((m['y'] as num?) ?? 0).toDouble();
    final button = '${m['b'] ?? 'left'}';
    lastX = x;
    lastY = y;
    switch (m['a']) {
      case 'down':
        if (button == 'right') {
          PhoneBridge.global('back');
        } else if (button == 'middle') {
          PhoneBridge.global('home');
        } else {
          downX = x;
          downY = y;
          downAt = DateTime.now().millisecondsSinceEpoch;
          path
            ..clear()
            ..addAll([x, y]);
        }
      case 'move':
        if (downX != null) {
          final px = path[path.length - 2], py = path[path.length - 1];
          if ((x - px).abs() + (y - py).abs() > 0.004) path.addAll([x, y]);
        }
      case 'up':
        if (button != 'left' || downX == null) return;
        final held = DateTime.now().millisecondsSinceEpoch - downAt;
        final moved = (x - downX!).abs() + (y - downY!).abs();
        if (moved < 0.01) {
          PhoneBridge.gesture([downX!, downY!], held >= 450 ? 700 : 40);
        } else {
          if (path.length < 4 || path[path.length - 2] != x || path[path.length - 1] != y) path.addAll([x, y]);
          PhoneBridge.gesture(List.of(path), held.clamp(120, 3000));
        }
        downX = downY = null;
    }
  }

  void _wheel(Map<String, dynamic> m) {
    final dy = ((m['dy'] as num?) ?? 0).toDouble();
    if (dy == 0) return;
    final dist = 0.18 * dy.sign;
    final y0 = (lastY - dist / 2).clamp(0.05, 0.95), y1 = (lastY + dist / 2).clamp(0.05, 0.95);
    PhoneBridge.gesture([lastX, y0, lastX, y1], 180);
  }

  void _flushTyping() {
    typeTimer?.cancel();
    typeTimer = null;
    if (typed.isEmpty) return;
    final text = typed.toString();
    typed.clear();
    PhoneBridge.type(text);
  }

  void _key(String k, String c) {
    switch (k) {
      case 'Escape':
        _flushTyping();
        PhoneBridge.global('back');
        return;
      case 'Return':
      case 'KP_Enter':
        _flushTyping();
        PhoneBridge.key('enter');
        return;
      case 'BackSpace':
        _flushTyping();
        PhoneBridge.key('backspace');
        return;
      case 'Super_L':
      case 'Super_R':
      case 'Win_L':
      case 'Win_R':
      case 'Home':
        _flushTyping();
        PhoneBridge.global('home');
        return;
    }
    if (c.isNotEmpty && c.runes.length == 1 && c.codeUnitAt(0) >= 32) {
      typed.write(c);
      typeTimer ??= Timer(const Duration(milliseconds: 120), _flushTyping);
    }
  }

  void _combo(List<String> keys) {
    _flushTyping();
    final set = keys.toSet();
    if (set.containsAll(['alt', 'tab'])) {
      PhoneBridge.global('recents');
    } else if (set.contains('cmd') && set.length == 1 || set.containsAll(['cmd', 'd']) || set.containsAll(['ctrl', 'esc'])) {
      PhoneBridge.global('home');
    } else if (set.containsAll(['cmd', 'l'])) {
      PhoneBridge.global('lock');
    } else if (set.containsAll(['alt', 'f4'])) {
      PhoneBridge.global('back');
    } else if (set.containsAll(['ctrl', 'c'])) {
      PhoneBridge.key('copy');
    } else if (set.containsAll(['ctrl', 'x'])) {
      PhoneBridge.key('cut');
    } else if (set.containsAll(['ctrl', 'v'])) {
      PhoneBridge.key('paste');
    } else if (set.containsAll(['ctrl', 'a'])) {
      PhoneBridge.key('selectall');
    } else if (set.containsAll(['cmd', 'n']) || set.containsAll(['cmd', 'a'])) {
      PhoneBridge.global('notifications');
    }
  }
}

class PhoneHost {
  final String id, password, name;
  final List<String> servers;
  final String? onlyAddress;
  final Map<String, (String, int)> _serverTargets = {};
  final status = ValueNotifier<String>('Starting...');
  final connected = ValueNotifier<bool>(false);
  void Function(String reason)? onStopped;
  Uint8List? _key;
  bool _running = false;
  Socket? _line;
  ServerSocket? _direct;
  _HostSession? _session;

  PhoneHost({required this.id, required this.password, required this.name, required this.servers, this.onlyAddress});

  bool get running => _running;

  Future<void> start() async {
    _running = true;
    status.value = 'Preparing...';
    _key = await _deriveKey(password, id);
    if (!_running) return;
    unawaited(_serverLoop());
    try {
      final at = onlyAddress != null ? InternetAddress(onlyAddress!) : InternetAddress.anyIPv4;
      _direct = await ServerSocket.bind(at, defaultPort, shared: true);
      _direct!.listen((s) => _serve(s, ByteReader(s)), onError: (_) {});
    } catch (_) {}
  }

  Future<void> stop([String reason = 'Stopped']) async {
    if (!_running) return;
    _running = false;
    _session?.close();
    _line?.destroy();
    await _direct?.close();
    _direct = null;
    await PhoneBridge.stopCapture();
    status.value = reason;
    connected.value = false;
    onStopped?.call(reason);
  }

  Future<List<String>> _ips() async {
    if (onlyAddress != null) return [onlyAddress!];
    final out = <String>[];
    try {
      for (final nic in await NetworkInterface.list(type: InternetAddressType.IPv4)) {
        for (final a in nic.addresses) {
          if (!a.isLoopback) out.add(a.address);
        }
      }
    } catch (_) {}
    return out.take(8).toList();
  }

  Future<void> _serverLoop() async {
    while (_running) {
      var refusedWait = false;
      for (final server in servers) {
        if (!_running) return;
        Socket? s;
        try {
          s = await _connectServer(server, const Duration(seconds: 4));
          _line = s;
          final r = ByteReader(s);
          s.add(utf8.encode('${jsonEncode({
            'op': 'hello', 'id': id, 'name': name, 'ips': await _ips(), 'port': defaultPort,
            'ver': appVersion, 'admin': false, 'os': 'Android',
          })}\n'));
          if (_session == null) status.value = 'Online on AnyKir Server - waiting for a PC';
          while (_running) {
            final msg = jsonDecode(await r.readLine()) as Map<String, dynamic>;
            switch (msg['op']) {
              case 'ping':
                s.add(utf8.encode('${jsonEncode({'op': 'pong'})}\n'));
              case 'open':
                unawaited(_openFromServer(server, '${msg['token'] ?? ''}'));
              case 'refused':
                refusedWait = true;
                status.value = 'AnyKir Server has disabled this phone';
                throw const SocketException('refused');
              case 'stop':
                await stop('Stopped from AnyKir Server');
                return;
            }
          }
        } catch (_) {
          s?.destroy();
          _line = null;
        }
        if (refusedWait) break;
      }
      if (!_running) return;
      if (_session == null && !refusedWait) status.value = 'AnyKir Server not reachable - retrying (direct connections still work)';
      await Future<void>.delayed(Duration(seconds: refusedWait ? 30 : 3));
    }
  }

  Future<void> _openFromServer(String server, String token) async {
    try {
      final s = await _connectServer(server, const Duration(seconds: 10));
      s.add(utf8.encode('${jsonEncode({'op': 'session', 'token': token})}\n'));
      await _serve(s, ByteReader(s));
    } catch (_) {}
  }

  Future<Socket> _connectServer(String server, Duration timeout) async {
    final (host, port) = parseAddress(server, serverPort);
    final only = onlyAddress;
    if (only == null) return openSocket(host, port, timeout: timeout);
    Future<Socket> from((String, int) to) async {
      final s = await Socket.connect(to.$1, to.$2, sourceAddress: only, timeout: timeout);
      s.setOption(SocketOption.tcpNoDelay, true);
      return s;
    }
    final target = _serverTargets[server] ?? (host, port);
    try {
      return await from(target);
    } catch (_) {
      final alt = await _serverAddressFor(host, port, only);
      if (alt == null || alt == target) rethrow;
      _serverTargets[server] = alt;
      return from(alt);
    }
  }

  static Future<(String, int)?> _serverAddressFor(String host, int port, String only) async {
    try {
      final s = await openSocket(host, port, timeout: const Duration(seconds: 4));
      try {
        s.add(utf8.encode('${jsonEncode({'op': 'where'})}\n'));
        final reply = jsonDecode(await ByteReader(s).readLine().timeout(const Duration(seconds: 4))) as Map;
        final ips = [for (final ip in (reply['ips'] as List? ?? [])) '$ip'];
        final prefix = only.substring(0, only.lastIndexOf('.') + 1);
        final same = ips.where((ip) => isTailscaleIp(only) ? isTailscaleIp(ip) : ip.startsWith(prefix));
        return same.isEmpty ? null : (same.first, port);
      } finally {
        s.destroy();
      }
    } catch (_) {
      return null;
    }
  }

  Future<void> _serve(Socket s, ByteReader r) async {
    final key = _key;
    if (!_running || key == null) {
      s.destroy();
      return;
    }
    SecureChannel ch;
    try {
      ch = await hostHandshake(s, r, key, id).timeout(const Duration(seconds: 20));
    } catch (_) {
      s.destroy();
      return;
    }
    _session?.close();
    final session = _HostSession(ch, this);
    _session = session;
    connected.value = true;
    status.value = 'A PC is connected';
    await session.run();
  }

  void _ended(_HostSession s) {
    if (_session != s) return;
    _session = null;
    connected.value = false;
    if (_running) status.value = 'Online - waiting for a PC';
  }

  void disconnectViewer() => _session?.close();
}

Future<Uint8List> _deriveKey(String password, String id) => Isolate.run(() => deriveKey(password, id));

(String, String) newPhoneIdentity() {
  final r = Random.secure();
  final id = '${100000000 + r.nextInt(899999999)}';
  const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  final pw = List.generate(10, (_) => chars[r.nextInt(chars.length)]).join();
  return (id, pw);
}
