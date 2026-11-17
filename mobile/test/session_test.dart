import 'dart:io';

import 'package:anykir_mobile/session.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final host = Platform.environment['ANYKIR_HOST'], pw = Platform.environment['ANYKIR_PW'];
  test('screen keeps flowing and survives a size change', () async {
    final s = await ViewerSession.open(address: host!, password: pw!);
    final refresh = Stream.periodic(const Duration(milliseconds: 300)).listen((_) => s.ch.sendJson({'t': 'refresh'}));
    try {
      await Future<void>.delayed(const Duration(seconds: 6));
      final first = s.frames;
      print('frames in 6 s: $first, screen ${s.width}x${s.height}, image ${s.image?.width}x${s.image?.height}');
      expect(first, greaterThan(6));
      expect(s.image, isNotNull);
      final w = s.width;
      s.setQuality(qualities[1]);
      await Future<void>.delayed(const Duration(seconds: 4));
      print('after quality change: frames ${s.frames}, screen ${s.width}x${s.height}, image ${s.image?.width}x${s.image?.height}');
      expect(s.width, isNot(w));
      expect(s.frames, greaterThan(first));
      expect(s.image, isNotNull);
      expect(s.closed.isCompleted, isFalse);
    } finally {
      await refresh.cancel();
      s.close();
    }
  }, skip: host == null ? 'set ANYKIR_HOST and ANYKIR_PW' : false, timeout: const Timeout(Duration(seconds: 60)));

  test('refresh resends the whole screen (heals spoiled pictures)', () async {
    final s = await ViewerSession.open(address: host!, password: pw!);
    try {
      await Future<void>.delayed(const Duration(seconds: 4));
      final before = s.frames;
      s.refresh();
      await Future<void>.delayed(const Duration(seconds: 2));
      print('pieces before refresh: $before, after: ${s.frames}');
      expect(s.frames, greaterThan(before));
      expect(s.image?.width, s.width);
    } finally {
      s.close();
    }
  }, skip: host == null ? 'set ANYKIR_HOST and ANYKIR_PW' : false, timeout: const Timeout(Duration(seconds: 30)));

  Future<String> pcClip() async =>
      ((await Process.run('python', ['-c', 'import pyperclip,sys; sys.stdout.write(pyperclip.paste())'])).stdout as String);
  Future<void> setPcClip(String t) => Process.run('python', ['-c', 'import pyperclip,sys; pyperclip.copy(sys.argv[1])', t]);

  test('copy and paste both ways', () async {
    final s = await ViewerSession.open(address: host!, password: pw!);
    final got = <String>[];
    final sub = s.clips.stream.listen(got.add);
    try {
      await Future<void>.delayed(const Duration(seconds: 2));
      expect(s.clipboardAllowed, isTrue);
      final toPc = 'anykir phone->pc ${DateTime.now().microsecondsSinceEpoch}';
      expect(s.sendClip(toPc), isTrue);
      await Future<void>.delayed(const Duration(milliseconds: 1500));
      expect(await pcClip(), toPc);
      expect(got, isNot(contains(toPc)), reason: 'no echo back to the phone');
      final toPhone = 'anykir pc->phone ${DateTime.now().microsecondsSinceEpoch}';
      await setPcClip(toPhone);
      await Future<void>.delayed(const Duration(seconds: 3));
      expect(got.last, toPhone);
      print('clipboard ok both ways');
    } finally {
      await sub.cancel();
      s.close();
    }
  }, skip: host == null ? 'set ANYKIR_HOST and ANYKIR_PW' : false, timeout: const Timeout(Duration(seconds: 60)));
}
