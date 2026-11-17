import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:cryptography/cryptography.dart';
import 'package:flutter/services.dart';

import 'version.dart';

const updatePublicKey = 'xcmp8bjQH/65z3xZz031DrEHMfnqRWSWPfjhLr+jIDk=';
const updateSources = ['http://192.168.210.112:7002', 'http://100.123.9.43:7002'];
const _channel = MethodChannel('anykir/update');
const _maxApk = 300 * 1024 * 1024;

class AppUpdate {
  final String version, sig, file, source;
  final int size;
  AppUpdate(this.version, this.sig, this.file, this.size, this.source);
}

List<int>? parseVersion(String text) {
  final m = RegExp(r'^(\d+)\.(\d+)\.(\d+)$').firstMatch(text.trim());
  return m == null ? null : [for (var i = 1; i <= 3; i++) int.parse(m.group(i)!)];
}

bool isNewer(String a, String b) {
  final pa = parseVersion(a), pb = parseVersion(b);
  if (pa == null || pb == null) return false;
  for (var i = 0; i < 3; i++) {
    if (pa[i] != pb[i]) return pa[i] > pb[i];
  }
  return false;
}

List<int> signedMessage(String version, String sha256Hex) => utf8.encode('anykir-android-v1|$version|$sha256Hex');

Future<bool> verifySignature(String version, String sha256Hex, String sig) async {
  try {
    final key = SimplePublicKey(base64.decode(updatePublicKey), type: KeyPairType.ed25519);
    return await Ed25519().verify(signedMessage(version, sha256Hex), signature: Signature(base64.decode(sig), publicKey: key));
  } catch (_) {
    return false;
  }
}

bool feedReached = false;

Future<AppUpdate?> checkForUpdate({List<String> sources = updateSources, String current = appVersion}) async {
  feedReached = false;
  for (final src in sources) {
    final client = HttpClient()..connectionTimeout = const Duration(seconds: 4);
    try {
      final req = await client.getUrl(Uri.parse('$src/android.json')).timeout(const Duration(seconds: 5));
      final res = await req.close().timeout(const Duration(seconds: 5));
      if (res.statusCode != 200) continue;
      final m = jsonDecode(await utf8.decodeStream(res).timeout(const Duration(seconds: 5))) as Map<String, dynamic>;
      feedReached = true;
      final v = '${m['version']}';
      if (!isNewer(v, current)) return null;
      return AppUpdate(v, '${m['sig']}', '${m['file'] ?? 'AnyKir.apk'}', (m['size'] as int?) ?? 0, src);
    } catch (_) {
      continue;
    } finally {
      client.close(force: true);
    }
  }
  return null;
}

Future<String> downloadUpdate(AppUpdate u, {String? dir, void Function(double)? progress}) async {
  dir ??= await _channel.invokeMethod<String>('dir');
  final path = '$dir${Platform.pathSeparator}AnyKir-${u.version}.apk';
  final client = HttpClient()..connectionTimeout = const Duration(seconds: 10);
  try {
    final req = await client.getUrl(Uri.parse('${u.source}/${Uri.encodeComponent(u.file)}'));
    final res = await req.close();
    if (res.statusCode != 200) throw Exception('download failed (${res.statusCode})');
    final total = res.contentLength > 0 ? res.contentLength : u.size;
    final out = File(path).openWrite();
    final hash = Sha256().newHashSink();
    var got = 0;
    try {
      await for (final chunk in res.timeout(const Duration(seconds: 30))) {
        got += chunk.length;
        if (got > _maxApk) throw Exception('update too large');
        out.add(chunk);
        hash.add(chunk);
        if (total > 0) progress?.call(got / total);
      }
    } finally {
      await out.close();
    }
    hash.close();
    final hex = (await hash.hash()).bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
    if (!await verifySignature(u.version, hex, u.sig)) {
      await File(path).delete();
      throw Exception('the update is not signed by AnyKir - not installed');
    }
    return path;
  } finally {
    client.close(force: true);
  }
}

void listenInstallStatus(void Function(String) onStatus) {
  _channel.setMethodCallHandler((call) async {
    if (call.method == 'status') onStatus('${call.arguments}');
  });
}

Future<bool> canReopen() async {
  try {
    return await _channel.invokeMethod<bool>('canReopen') ?? true;
  } catch (_) {
    return true;
  }
}

Future<bool> meteredNetwork() async {
  try {
    return await _channel.invokeMethod<bool>('meteredNetwork') ?? false;
  } catch (_) {
    return false;
  }
}

Future<void> allowReopen() async {
  try {
    await _channel.invokeMethod('allowReopen');
  } catch (_) {}
}

Future<String> installApk(String path) async =>
    await _channel.invokeMethod<String>('install', {'path': path}) ?? 'started';
