import 'package:flutter/services.dart';

const _channel = MethodChannel('anykir/clipboard');

Future<int?> clipStamp() async {
  try {
    final v = await _channel.invokeMethod<int>('stamp');
    return v == null || v == -2 ? null : v;
  } catch (_) {
    return null;
  }
}

Future<String?> readClipText() async {
  try {
    return (await Clipboard.getData(Clipboard.kTextPlain))?.text;
  } catch (_) {
    return null;
  }
}

Future<void> writeClipText(String text) => Clipboard.setData(ClipboardData(text: text));
