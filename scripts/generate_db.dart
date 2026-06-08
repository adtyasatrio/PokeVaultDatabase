// ignore_for_file: avoid_print

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:image/image.dart' as img;
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

const _apiHost = 'api.pokemontcg.io';
const _apiPath = '/v2/cards';
const _defaultDbPath = 'assets/db/poc.db';
const _defaultPageSize = 250;
const _defaultConcurrency = 24;

Future<void> main(List<String> args) async {
  try {
    await _run(args);
  } on UsageException catch (error) {
    stderr.writeln(error.message);
    stderr.writeln('');
    printUsage();
    exitCode = 64;
  }
}

Future<void> _run(List<String> args) async {
  final config = PipelineConfig.parse(args, Platform.environment);

  sqfliteFfiInit();
  final db = await _openOfflineDb(config.dbPath);
  final client = HttpClient()..connectionTimeout = config.timeout;

  var created = 0;
  var updated = 0;
  var skipped = 0;
  var failed = 0;
  var seen = 0;

  try {
    await _ensureSchema(db);
    final existingIds = config.force
        ? <String>{}
        : (await db.query(
            'cards',
            columns: ['id'],
          )).map((row) => row['id'] as String).toSet();

    print('Offline DB pipeline');
    print('db=${config.dbPath}');
    print(
      'pageSize=${config.pageSize} concurrency=${config.concurrency} force=${config.force}',
    );
    if (config.query != null) {
      print('query=${config.query}');
    }
    if (config.limit != null) {
      print('limit=${config.limit}');
    }
    print('existing cards=${existingIds.length}');

    var page = 1;
    var reachedEnd = false;

    while (!reachedEnd) {
      final response = await _fetchCardsPage(client, config, page);
      final cards = response.cards;
      if (cards.isEmpty) break;

      final remaining = config.limit == null ? null : config.limit! - seen;
      final pageCards = remaining == null || remaining >= cards.length
          ? cards
          : cards.take(remaining).toList();

      final newCards = <TcgCard>[];
      for (final card in pageCards) {
        seen++;
        if (!config.force && existingIds.contains(card.id)) {
          skipped++;
        } else if (card.imageUrl == null || card.imageUrl!.isEmpty) {
          failed++;
          print('missing image: ${card.id} ${card.name}');
        } else {
          newCards.add(card);
        }
      }

      // No image download needed — just save metadata + image_url.
      // TFLite embeddings are generated separately via add_tflite_emb.py.
      await db.transaction((txn) async {
        for (final card in newCards) {
          if (card.imageUrl == null || card.imageUrl!.isEmpty) {
            failed++;
            continue;
          }
          final record = OfflineCardRecord.fromCard(card);
          final existed = existingIds.contains(record.id);
          await txn.insert(
            'cards',
            record.toSqlite(),
            conflictAlgorithm: ConflictAlgorithm.replace,
          );
          if (existed) {
            updated++;
          } else {
            created++;
            existingIds.add(record.id);
          }
        }
      });

      print(
        'page=$page cards=${newCards.length} created=$created updated=$updated skipped=$skipped failed=$failed',
      );

      final totalCount = response.totalCount;
      final hasServerTotal = totalCount != null && totalCount > 0;
      if (!config.force &&
          config.limit == null &&
          hasServerTotal &&
          existingIds.length >= totalCount) {
        print(
          'local DB already has ${existingIds.length}/$totalCount cards; stopping metadata fetch',
        );
        reachedEnd = true;
        continue;
      }

      reachedEnd =
          cards.length < config.pageSize ||
          (hasServerTotal && page * config.pageSize >= totalCount) ||
          (config.limit != null && seen >= config.limit!);
      page++;
    }

    await db.insert('metadata', {
      'key': 'generated_at',
      'value': DateTime.now().toUtc().toIso8601String(),
    }, conflictAlgorithm: ConflictAlgorithm.replace);
    await db.insert('metadata', {
      'key': 'source',
      'value': 'Pokemon TCG API',
    }, conflictAlgorithm: ConflictAlgorithm.replace);
    await db.insert('metadata', {
      'key': 'schema_version',
      'value': '2',
    }, conflictAlgorithm: ConflictAlgorithm.replace);

    await db.execute('VACUUM');

    if (config.assetPath != null && config.assetPath != config.dbPath) {
      await _copyDbToAsset(config.dbPath, config.assetPath!);
    }

    final countRows = await db.rawQuery('SELECT COUNT(*) AS count FROM cards');
    final count = countRows.first['count'] as int? ?? 0;
    print(
      'Done. cards=$count created=$created updated=$updated skipped=$skipped failed=$failed',
    );
  } finally {
    client.close(force: true);
    await db.close();
  }
}


Future<Database> _openOfflineDb(String dbPath) async {
  final file = File(dbPath).absolute;
  await file.parent.create(recursive: true);
  return databaseFactoryFfi.openDatabase(file.path);
}

Future<void> _ensureSchema(Database db) async {
  await db.execute('''
      CREATE TABLE IF NOT EXISTS cards (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          set_name TEXT,
          card_number TEXT,
          hp TEXT
      )
  ''');
  await db.execute('''
      CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
      )
  ''');

  final columns = await db.rawQuery('PRAGMA table_info(cards)');
  final existing = columns.map((row) => row['name'] as String).toSet();
  final additions = <String, String>{
    'set_id': 'TEXT',
    'set_code': 'TEXT',
    'image_url': 'TEXT',
    'updated_at': 'TEXT',
    'rarity': 'TEXT',
    'types': 'TEXT',
  };

  for (final entry in additions.entries) {
    if (!existing.contains(entry.key)) {
      await db.execute(
        'ALTER TABLE cards ADD COLUMN ${entry.key} ${entry.value}',
      );
    }
  }

  await db.execute('CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name)');
  await db.execute(
    'CREATE INDEX IF NOT EXISTS idx_cards_set_number ON cards(set_id, card_number)',
  );
}

Future<TcgPage> _fetchCardsPage(
  HttpClient client,
  PipelineConfig config,
  int page,
) async {
  final params = <String, String>{
    'page': '$page',
    'pageSize': '${config.pageSize}',
    'select': 'id,name,hp,number,set,images,rarity,types',
    'orderBy': 'set.releaseDate,number',
    if (config.query != null) 'q': config.query!,
  };
  final uri = Uri.https(_apiHost, _apiPath, params);
  final json = await _getJson(client, uri, config);
  final data = json['data'] as List? ?? const [];
  final cards = data
      .whereType<Map<String, dynamic>>()
      .map(TcgCard.fromJson)
      .toList(growable: false);
  final totalCount = (json['totalCount'] as num?)?.toInt();
  print('fetched page=$page cards=${cards.length} total=${totalCount ?? '?'}');
  return TcgPage(cards: cards, totalCount: totalCount);
}

Future<Map<String, dynamic>> _getJson(
  HttpClient client,
  Uri uri,
  PipelineConfig config,
) async {
  final body = await _getBytes(client, uri, config);
  return jsonDecode(utf8.decode(body)) as Map<String, dynamic>;
}

Future<Uint8List> _getBytes(
  HttpClient client,
  Uri uri,
  PipelineConfig config,
) async {
  Object? lastError;

  for (var attempt = 1; attempt <= config.retries; attempt++) {
    try {
      final request = await client.getUrl(uri);
      request.headers.set(HttpHeaders.acceptHeader, '*/*');
      if (config.apiKey != null && uri.host == _apiHost) {
        request.headers.set('X-Api-Key', config.apiKey!);
      }

      final response = await request.close().timeout(config.timeout);
      final bytes = await consolidateHttpClientResponseBytes(response);

      if (response.statusCode >= 200 && response.statusCode < 300) {
        return bytes;
      }

      final message = utf8.decode(bytes, allowMalformed: true);
      throw HttpException(
        'HTTP ${response.statusCode} for $uri: $message',
        uri: uri,
      );
    } catch (error) {
      lastError = error;
      if (attempt == config.retries) break;
      print(
        'request retry attempt=$attempt/${config.retries} uri=$uri error=$error',
      );
      await Future<void>.delayed(Duration(seconds: attempt * 2));
    }
  }

  throw lastError ?? StateError('Request failed for $uri');
}

Future<void> _copyDbToAsset(String dbPath, String assetPath) async {
  final source = File(dbPath);
  final target = File(assetPath);
  await target.parent.create(recursive: true);
  await source.copy(target.path);
  print('copied ${source.path} -> ${target.path}');
}

Future<Uint8List> consolidateHttpClientResponseBytes(
  HttpClientResponse response,
) async {
  final builder = BytesBuilder(copy: false);
  await for (final chunk in response) {
    builder.add(chunk);
  }
  return builder.takeBytes();
}

class PipelineConfig {
  const PipelineConfig({
    required this.dbPath,
    required this.pageSize,
    required this.concurrency,
    required this.force,
    required this.timeout,
    required this.retries,
    this.assetPath,
    this.apiKey,
    this.limit,
    this.query,
  });

  final String dbPath;
  final String? assetPath;
  final String? apiKey;
  final String? query;
  final int pageSize;
  final int concurrency;
  final int? limit;
  final int retries;
  final bool force;
  final Duration timeout;

  static PipelineConfig parse(List<String> args, Map<String, String> env) {
    final values = <String, String>{};
    final flags = <String>{};

    for (var i = 0; i < args.length; i++) {
      final arg = args[i];
      if (!arg.startsWith('--')) {
        throw UsageException('Unknown argument: $arg');
      }

      final withoutPrefix = arg.substring(2);
      final splitIndex = withoutPrefix.indexOf('=');
      if (splitIndex >= 0) {
        values[withoutPrefix.substring(0, splitIndex)] = withoutPrefix
            .substring(splitIndex + 1);
      } else if (_booleanFlags.contains(withoutPrefix)) {
        flags.add(withoutPrefix);
      } else {
        if (i + 1 >= args.length) {
          throw UsageException('Missing value for --$withoutPrefix');
        }
        values[withoutPrefix] = args[++i];
      }
    }

    if (flags.contains('help')) {
      printUsage();
      exit(0);
    }

    final pageSize = _parseInt(values['page-size'], _defaultPageSize);
    if (pageSize < 1 || pageSize > 250) {
      throw UsageException('--page-size must be between 1 and 250');
    }

    final concurrency = _parseInt(values['concurrency'], _defaultConcurrency);
    if (concurrency < 1 || concurrency > 80) {
      throw UsageException('--concurrency must be between 1 and 80');
    }

    final limit = values.containsKey('limit')
        ? _parseInt(values['limit'], 0)
        : null;
    if (limit != null && limit < 1) {
      throw UsageException('--limit must be greater than 0');
    }

    return PipelineConfig(
      dbPath: values['db-path'] ?? _defaultDbPath,
      assetPath: values['asset-path'],
      apiKey: values['api-key'] ?? env['POKEMON_TCG_API_KEY'],
      query: values['query'],
      pageSize: pageSize,
      concurrency: concurrency,
      limit: limit,
      retries: _parseInt(values['retries'], 3),
      timeout: Duration(seconds: _parseInt(values['timeout-seconds'], 45)),
      force: flags.contains('force'),
    );
  }

  static int _parseInt(String? value, int fallback) {
    if (value == null) return fallback;
    final parsed = int.tryParse(value);
    if (parsed == null) {
      throw UsageException('Expected integer but got "$value"');
    }
    return parsed;
  }
}

class TcgPage {
  const TcgPage({required this.cards, required this.totalCount});

  final List<TcgCard> cards;
  final int? totalCount;
}

class TcgCard {
  const TcgCard({
    required this.id,
    required this.name,
    required this.number,
    required this.setId,
    required this.setName,
    required this.setCode,
    required this.printedTotal,
    required this.hp,
    required this.imageUrl,
    required this.rarity,
    required this.types,
  });

  final String id;
  final String name;
  final String number;
  final String? setId;
  final String? setName;
  final String? setCode;
  final int? printedTotal;
  final String? hp;
  final String? imageUrl;
  final String? rarity;
  final String? types;

  factory TcgCard.fromJson(Map<String, dynamic> json) {
    final set = json['set'] as Map<String, dynamic>? ?? const {};
    final images = json['images'] as Map<String, dynamic>? ?? const {};
    final number = json['number'] as String? ?? '';
    return TcgCard(
      id: json['id'] as String,
      name: json['name'] as String? ?? 'Unknown',
      number: number,
      setId: set['id'] as String?,
      setName: set['name'] as String?,
      setCode: set['ptcgoCode'] as String?,
      printedTotal: (set['printedTotal'] as num?)?.toInt(),
      hp: json['hp'] as String?,
      imageUrl: images['large'] as String? ?? images['small'] as String?,
      rarity: json['rarity'] as String?,
      types: (json['types'] as List?)?.join(', '),
    );
  }

  String get displayNumber {
    final total = printedTotal;
    if (total == null || number.contains('/')) return number;
    return '$number/$total';
  }
}

class OfflineCardRecord {
  const OfflineCardRecord({
    required this.id,
    required this.name,
    required this.setName,
    required this.cardNumber,
    required this.hp,
    required this.setId,
    required this.setCode,
    required this.imageUrl,
    required this.updatedAt,
    required this.rarity,
    required this.types,
  });

  final String id;
  final String name;
  final String? setName;
  final String cardNumber;
  final String? hp;
  final String? setId;
  final String? setCode;
  final String? imageUrl;
  final String updatedAt;
  final String? rarity;
  final String? types;

  factory OfflineCardRecord.fromCard(
    TcgCard card,
  ) {
    return OfflineCardRecord(
      id: card.id,
      name: card.name,
      setName: card.setName,
      cardNumber: card.displayNumber,
      hp: card.hp,
      setId: card.setId,
      setCode: card.setCode,
      imageUrl: card.imageUrl,
      updatedAt: DateTime.now().toUtc().toIso8601String(),
      rarity: card.rarity,
      types: card.types,
    );
  }

  Map<String, Object?> toSqlite() {
    return {
      'id': id,
      'name': name,
      'set_name': setName,
      'card_number': cardNumber,
      'hp': hp,
      'set_id': setId,
      'set_code': setCode,
      'image_url': imageUrl,
      'updated_at': updatedAt,
      'rarity': rarity,
      'types': types,
    };
  }
}

class UsageException implements Exception {
  const UsageException(this.message);

  final String message;

  @override
  String toString() => message;
}

const _booleanFlags = {'force', 'help'};

void printUsage() {
  print('''
Usage:
  dart run generate_db.dart [options]

Options:
  --db-path <path>           SQLite output path. Default: $_defaultDbPath
  --asset-path <path>        Optional copy target after generation.
  --api-key <key>            Pokemon TCG API key. Can also use POKEMON_TCG_API_KEY.
  --query <q>                Optional Pokemon TCG API q filter.
  --limit <count>            Stop after this many fetched cards. Useful for smoke tests.
  --page-size <count>        API page size, 1-250. Default: $_defaultPageSize
  --concurrency <count>      Parallel image downloads per batch. Default: $_defaultConcurrency
  --retries <count>          Retries per request. Default: 3
  --timeout-seconds <count>  Request timeout. Default: 45
  --force                    Rehash cards already present in the DB.
  --help                     Show this help.

Examples:
  dart run generate_db.dart --limit 25
  dart run generate_db.dart --query 'set.id:sv8'
  POKEMON_TCG_API_KEY=... dart run generate_db.dart --concurrency 32
''');
}
