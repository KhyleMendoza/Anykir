import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'protocol.dart';

class Quality {
  final String name;
  final int quality, fps;
  final double scale;
  const Quality(this.name, this.quality, this.scale, this.fps);
  Map<String, Object> get cfg => {'t': 'cfg', 'quality': quality, 'scale': scale, 'fps': fps};
}

const qualities = [
  Quality('Smooth (default)', 65, 1.0, 30),
  Quality('Lighter (saves data)', 50, 0.6, 20),
  Quality('Best quality', 85, 1.0, 20),
  Quality('Slow connection', 35, 0.5, 10),
];

bool healDue(DateTime now, DateTime lastHeal, DateTime lastFrame, {required bool metered}) {
  if (now.difference(lastHeal) < Duration(seconds: metered ? 45 : 10)) return false;
  return lastFrame.isAfter(lastHeal.add(const Duration(seconds: 2)));
}

class ViewerSession {
  final SecureChannel ch;
  final String? pcId;
  ui.Image? image;
  int width = 0, height = 0, rx = 0, frames = 0;
  DateTime lastFrameAt = DateTime(0);
  String hostName = '', warning = '';
  bool inputAllowed = true, adminAccess = false, clipboardAllowed = true;
  String lastClip = '';
  final clips = StreamController<String>.broadcast();
  double cursorX = -1, cursorY = -1;
  String cursorShape = '';
  bool caret = false;
  bool cursorVisible = false;
  int monitorCount = 1, monitor = 1;
  final changes = StreamController<void>.broadcast();
  final notices = StreamController<String>.broadcast();
  final closed = Completer<String>();
  final List<(ui.Image, int, int)> _tiles = [];
  Future<void> _decodeChain = Future.value();
  bool _flushQueued = false;
  int _generation = 0, _decoding = 0;
  (double, double)? _pendingMove;
  Timer? _moveTimer;

  ViewerSession._(this.ch, this.pcId);

  static Future<ViewerSession> open({
    required String address,
    required String password,
    List<String> servers = const [],
    String? pcId,
    String? tailscale,
    Quality? quality,
  }) async {
    quality ??= qualities.first;
    final routes = <Future<(Socket, ByteReader)> Function()>[
      if (pcId != null)
        for (final server in servers) () => connectVia(server, pcId),
      if (tailscale != null) () => _direct(tailscale),
      if (pcId == null || tailscale == null) () => _direct(address),
    ];
    Object error = ConnectError('Could not connect');
    for (final route in routes) {
      Socket socket;
      ByteReader reader;
      try {
        (socket, reader) = await route();
      } on SocketException catch (e) {
        error = ConnectError('Could not connect: ${e.osError?.message ?? e.message}');
        continue;
      } on TimeoutException {
        error = ConnectError('Could not connect: timed out');
        continue;
      } on ConnectError catch (e) {
        error = e;
        continue;
      }
      try {
        final ch = await clientHandshake(socket, reader, password).timeout(const Duration(seconds: 30));
        final s = ViewerSession._(ch, pcId);
        await ch.sendJson({'t': 'caps', 'ack': 1});
        await ch.sendJson(quality.cfg);
        s._run();
        return s;
      } on AuthError {
        socket.destroy();
        rethrow;
      } catch (e) {
        socket.destroy();
        error = e is TimeoutException ? ConnectError('Could not connect: timed out') : e;
      }
    }
    throw error;
  }

  static Future<(Socket, ByteReader)> _direct(String address) async {
    final (host, port) = parseAddress(address, defaultPort);
    final socket = await openSocket(host, port);
    return (socket, ByteReader(socket));
  }

  void _notify() {
    if (!changes.isClosed) changes.add(null);
  }

  Future<void> _run() async {
    var reason = 'Connection closed';
    try {
      while (true) {
        final (type, body) = await ch.recv();
        if (type == tFrame) {
          final head = ByteData.sublistView(body, 0, 8);
          final x = head.getUint16(0), y = head.getUint16(2);
          final jpeg = Uint8List.sublistView(body, 8);
          final n = ++rx, gen = _generation;
          _decoding++;
          _decodeChain = _decodeChain.then((_) async {
            await _addTile(jpeg, x, y, gen);
            _decoding--;
            if (!ch.closed) ch.sendJson({'t': 'ack', 'n': n});
          });
        } else if (type == tJson) {
          _onMessage(jsonDecode(utf8.decode(body)) as Map<String, dynamic>);
        }
      }
    } catch (e) {
      reason = e.toString();
    }
    ch.close();
    if (!closed.isCompleted) closed.complete(reason);
    _notify();
  }

  void _onMessage(Map<String, dynamic> m) {
    switch (m['t']) {
      case 'hello':
        hostName = '${m['host'] ?? ''}';
        inputAllowed = m['input'] != false;
        adminAccess = m['uac'] == true;
        clipboardAllowed = m['clipboard'] != false;
      case 'screen':
        width = m['w'] as int;
        height = m['h'] as int;
        monitorCount = (m['count'] as int?) ?? 1;
        monitor = (m['mon'] as int?) ?? 1;
        _generation++;
        for (final (tile, _, _) in _tiles) {
          tile.dispose();
        }
        _tiles.clear();
      case 'cur':
        cursorVisible = m['vis'] != false;
        cursorX = (m['x'] as num?)?.toDouble() ?? -1;
        cursorY = (m['y'] as num?)?.toDouble() ?? -1;
        cursorShape = '${m['s'] ?? ''}';
      case 'caret':
        caret = m['on'] == true;
      case 'warn':
        warning = m['locked'] == true
            ? 'The PC is locked. To unlock it from here, install the admin service on that PC.'
            : (m['blocked'] == true ? 'That window runs as administrator; install the admin service on the PC.' : '');
      case 'capture_status':
        warning = m['ok'] == false ? 'Screen capture recovering...' : '';
      case 'clip':
        final text = '${m['text'] ?? ''}';
        if (text.isNotEmpty && text != lastClip) {
          lastClip = text;
          if (!clips.isClosed) clips.add(text);
        }
      case 'notice':
        if (!notices.isClosed) notices.add('${m['text'] ?? ''}');
    }
    _notify();
  }

  Future<void> _addTile(Uint8List jpeg, int x, int y, int gen) async {
    try {
      final codec = await ui.instantiateImageCodec(jpeg);
      final frame = await codec.getNextFrame();
      codec.dispose();
      if (gen != _generation) {
        frame.image.dispose();
        return;
      }
      _tiles.add((frame.image, x, y));
      frames++;
      lastFrameAt = DateTime.now();
      if (_decoding <= 1) {
        _flush();
      } else if (!_flushQueued) {
        _flushQueued = true;
        Timer(const Duration(milliseconds: 50), _flush);
      }
    } catch (_) {}
  }

  bool _flushing = false, _flushAgain = false;

  Future<void> _flush() async {
    _flushQueued = false;
    if (_flushing) {
      _flushAgain = true;
      return;
    }
    if (width <= 0 || height <= 0 || _tiles.isEmpty) return;
    _flushing = true;
    try {
      final gen = _generation;
      final tiles = List.of(_tiles);
      _tiles.clear();
      final old = image;
      final recorder = ui.PictureRecorder();
      final canvas = ui.Canvas(recorder);
      if (old != null) {
        if (old.width == width && old.height == height) {
          canvas.drawImage(old, ui.Offset.zero, ui.Paint());
        } else {
          canvas.drawImageRect(old, ui.Rect.fromLTWH(0, 0, old.width.toDouble(), old.height.toDouble()),
              ui.Rect.fromLTWH(0, 0, width.toDouble(), height.toDouble()), ui.Paint()..filterQuality = ui.FilterQuality.low);
        }
      }
      for (final (tile, x, y) in tiles) {
        canvas.drawImage(tile, ui.Offset(x.toDouble(), y.toDouble()), ui.Paint());
      }
      final picture = recorder.endRecording();
      final next = await picture.toImage(width, height);
      picture.dispose();
      for (final (tile, _, _) in tiles) {
        tile.dispose();
      }
      if (gen != _generation || closed.isCompleted) {
        next.dispose();
        return;
      }
      image = next;
      old?.dispose();
      _notify();
    } catch (_) {
    } finally {
      _flushing = false;
      if (_flushAgain || _tiles.isNotEmpty) {
        _flushAgain = false;
        if (_tiles.isNotEmpty) _flush();
      }
    }
  }

  bool viewOnly = false;

  void Function(String kind)? debugInput;

  bool _blocked(String kind) {
    if (viewOnly) return true;
    debugInput?.call(kind);
    return false;
  }

  void mouse(String action, double x, double y, {String button = 'left'}) {
    if (_blocked('mouse')) return;
    x = x.clamp(0.0, 1.0);
    y = y.clamp(0.0, 1.0);
    if (action == 'move') {
      _sendMove(x, y);
      return;
    }
    _moveTimer?.cancel();
    _moveTimer = null;
    _pendingMove = null;
    ch.sendJson({'t': 'mouse', 'a': action, 'x': x, 'y': y, 'b': button});
  }

  void _sendMove(double x, double y) {
    if (_moveTimer != null) {
      _pendingMove = (x, y);
      return;
    }
    ch.sendJson({'t': 'mouse', 'a': 'move', 'x': x, 'y': y});
    _moveTimer = Timer(const Duration(milliseconds: 16), () {
      _moveTimer = null;
      final p = _pendingMove;
      _pendingMove = null;
      if (p != null) _sendMove(p.$1, p.$2);
    });
  }

  void click(double x, double y, {String button = 'left'}) {
    mouse('move', x, y);
    mouse('down', x, y, button: button);
    mouse('up', x, y, button: button);
  }

  void wheel(int dy) {
    if (!_blocked('wheel')) ch.sendJson({'t': 'wheel', 'dx': 0, 'dy': dy});
  }

  void Function(String keysym)? debugKeySent;

  void key(String keysym, {String char = ''}) {
    if (_blocked('key')) return;
    debugKeySent?.call(keysym);
    ch.sendJson({'t': 'key', 'a': 'down', 'k': keysym, 'c': char});
    ch.sendJson({'t': 'key', 'a': 'up', 'k': keysym, 'c': char});
  }

  void typeText(String text) {
    for (final rune in text.runes) {
      final c = String.fromCharCode(rune);
      if (c == '\n') {
        key('Return');
      } else {
        key(c, char: c);
      }
    }
  }

  void combo(List<String> keys) {
    if (!_blocked('combo')) ch.sendJson({'t': 'combo', 'keys': keys});
  }

  void lock() {
    if (!_blocked('lock')) ch.sendJson({'t': 'lock'});
  }

  bool sendClip(String text) {
    if (viewOnly || !clipboardAllowed || text.isEmpty || text == lastClip || text.length > 1000000) return false;
    lastClip = text;
    ch.sendJson({'t': 'clip', 'text': text});
    return true;
  }

  void ctrlAltDel() {
    if (!_blocked('cad')) ch.sendJson({'t': 'cad'});
  }

  void setMonitor(int index) => ch.sendJson({'t': 'cfg', 'monitor': index});

  void setQuality(Quality q) => ch.sendJson(q.cfg);

  void refresh() {
    if (!ch.closed) ch.sendJson({'t': 'refresh'});
  }

  void close() {
    _moveTimer?.cancel();
    ch.close();
    if (!closed.isCompleted) closed.complete('Disconnected');
  }
}
