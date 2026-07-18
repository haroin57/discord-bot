import express from 'express';
import cors from 'cors';
import { spawn } from 'child_process';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { isInitializeRequest, } from '@modelcontextprotocol/sdk/types.js';
import { getVersion } from '../lib/getVersion.js';
import { onSignals } from '../lib/onSignals.js';
import { serializeCorsOrigin } from '../lib/serializeCorsOrigin.js';
const setResponseHeaders = ({ res, headers, }) => Object.entries(headers).forEach(([key, value]) => {
    res.setHeader(key, value);
});
// Helper function to create initialize request
const createInitializeRequest = (id, protocolVersion) => ({
    jsonrpc: '2.0',
    id,
    method: 'initialize',
    params: {
        protocolVersion,
        capabilities: {
            roots: {
                listChanged: true,
            },
            sampling: {},
        },
        clientInfo: {
            name: 'supergateway',
            version: getVersion(),
        },
    },
});
// Helper function to create initialized notification
const createInitializedNotification = () => ({
    jsonrpc: '2.0',
    method: 'notifications/initialized',
});
export async function stdioToStatelessStreamableHttp(args) {
    const { stdioCmd, port, streamableHttpPath, logger, corsOrigin, healthEndpoints, headers, protocolVersion, } = args;
    logger.info(`  - Headers: ${Object(headers).length ? JSON.stringify(headers) : '(none)'}`);
    logger.info(`  - port: ${port}`);
    logger.info(`  - stdio: ${stdioCmd}`);
    logger.info(`  - streamableHttpPath: ${streamableHttpPath}`);
    logger.info(`  - protocolVersion: ${protocolVersion}`);
    logger.info(`  - CORS: ${corsOrigin ? `enabled (${serializeCorsOrigin({ corsOrigin })})` : 'disabled'}`);
    logger.info(`  - Health endpoints: ${healthEndpoints.length ? healthEndpoints.join(', ') : '(none)'}`);
    onSignals({ logger });
    const app = express();
    // LOCAL PATCH (2026-07-10, docs/oracle_mcp.md 追補5): express.json() の
    // デフォルト 100kb 制限だと長い consult prompt が 413 で弾かれるため拡大。
    app.use(express.json({ limit: '8mb' }));
    if (corsOrigin) {
        app.use(cors({ origin: corsOrigin }));
    }
    for (const ep of healthEndpoints) {
        app.get(ep, (_req, res) => {
            setResponseHeaders({
                res,
                headers,
            });
            res.send('ok');
        });
    }
    app.post(streamableHttpPath, async (req, res) => {
        // In stateless mode, create a new instance of transport and server for each request
        // to ensure complete isolation. A single instance would cause request ID collisions
        // when multiple clients connect concurrently.
        try {
            const server = new Server({ name: 'supergateway', version: getVersion() }, { capabilities: {} });
            const transport = new StreamableHTTPServerTransport({
                sessionIdGenerator: undefined,
            });
            await server.connect(transport);
            const child = spawn(stdioCmd, { shell: true, detached: true });
            // LOCAL PATCH (2026-07-05, ADR-0037): stateless gateway child-leak fix.
            // shell:true spawns the real MCP as a GRANDCHILD; child.kill() only
            // hits the wrapper shell, so a long-running grandchild (oracle-mcp's
            // 10-60min background consult) orphans to PPid=1 and piles up
            // (measured 37 procs / 2GB RSS / 1.8G swap). detached:true makes the
            // child a process-group leader so we can signal the WHOLE group.
            let _killed = false;
            const killTree = (reason) => {
                if (_killed)
                    return;
                _killed = true;
                logger.info(`Reaping child group pgid=${child.pid} reason=${reason}`);
                try {
                    process.kill(-child.pid, 'SIGTERM');
                }
                catch {
                    try {
                        child.kill('SIGTERM');
                    }
                    catch { }
                }
                setTimeout(() => {
                    try {
                        process.kill(-child.pid, 'SIGKILL');
                    }
                    catch { }
                }, 15000).unref();
            };
            child.on('exit', (code, signal) => {
                logger.error(`Child exited: code=${code}, signal=${signal}`);
                transport.close();
            });
            // State tracking for initialization flow
            let isInitialized = false;
            let initializeRequestId = null; // Current initialize request ID
            let isAutoInitializing = false; // Flag to indicate if we're auto-initializing
            let pendingOriginalMessage = null;
            let buffer = '';
            child.stdout.on('data', (chunk) => {
                buffer += chunk.toString('utf8');
                const lines = buffer.split(/\r?\n/);
                buffer = lines.pop() ?? '';
                lines.forEach((line) => {
                    if (!line.trim())
                        return;
                    try {
                        const jsonMsg = JSON.parse(line);
                        logger.info('Child → StreamableHttp:', line);
                        // Handle initialize response (both auto and client initiated)
                        if (initializeRequestId && jsonMsg.id === initializeRequestId) {
                            logger.info('Initialize response received');
                            isInitialized = true;
                            // If this was our auto-initialization, send initialized notification and pending message
                            if (isAutoInitializing) {
                                // Send initialized notification
                                const initializedNotification = createInitializedNotification();
                                logger.info(`StreamableHttp → Child (initialized): ${JSON.stringify(initializedNotification)}`);
                                child.stdin.write(JSON.stringify(initializedNotification) + '\n');
                                // Now send the original message
                                if (pendingOriginalMessage) {
                                    logger.info(`StreamableHttp → Child (original): ${JSON.stringify(pendingOriginalMessage)}`);
                                    child.stdin.write(JSON.stringify(pendingOriginalMessage) + '\n');
                                    pendingOriginalMessage = null;
                                }
                                // Reset auto-initialize tracking
                                isAutoInitializing = false;
                                initializeRequestId = null;
                                // Don't forward our auto-initialize response to the client
                                return;
                            }
                            else {
                                // Client-initiated initialize response, just reset tracking
                                initializeRequestId = null;
                            }
                        }
                        try {
                            // LOCAL PATCH (2026-07-04, docs/oracle_mcp.md): transport.send は
                            // async。クライアント切断後の応答で unhandled rejection になり
                            // node ごと死ぬ (No connection established for request ID) ため
                            // rejection を捕捉してログに落とす。
                            const sendResult = transport.send(jsonMsg);
                            if (sendResult && typeof sendResult.catch === 'function') {
                                sendResult.catch((e) => {
                                    logger.error(`Failed to send to StreamableHttp (async)`, e);
                                });
                            }
                        }
                        catch (e) {
                            logger.error(`Failed to send to StreamableHttp`, e);
                        }
                    }
                    catch {
                        logger.error(`Child non-JSON: ${line}`);
                    }
                });
            });
            child.stderr.on('data', (chunk) => {
                logger.error(`Child stderr: ${chunk.toString('utf8')}`);
            });
            transport.onmessage = (msg) => {
                logger.info(`StreamableHttp → Child: ${JSON.stringify(msg)}`);
                // Check if we need to auto-initialize first
                if (!isInitialized && !isInitializeRequest(msg)) {
                    // Store the original message and send initialize first
                    pendingOriginalMessage = msg;
                    initializeRequestId = `init_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
                    isAutoInitializing = true;
                    logger.info('Non-initialize message detected, sending auto-initialize request first');
                    const initRequest = createInitializeRequest(initializeRequestId, protocolVersion);
                    logger.info(`StreamableHttp → Child (auto-initialize): ${JSON.stringify(initRequest)}`);
                    child.stdin.write(JSON.stringify(initRequest) + '\n');
                    // Don't send the original message yet - it will be sent after initialization
                    return;
                }
                // Track initialize request ID (both client and auto)
                if (isInitializeRequest(msg) && 'id' in msg && msg.id !== undefined) {
                    initializeRequestId = msg.id;
                    isAutoInitializing = false; // This is client-initiated
                    logger.info(`Tracking initialize request ID: ${msg.id}`);
                }
                // Send all messages to child process normally
                child.stdin.write(JSON.stringify(msg) + '\n');
            };
            transport.onclose = () => {
                logger.info('StreamableHttp connection closed');
                killTree('onclose');
            };
            transport.onerror = (err) => {
                logger.error(`StreamableHttp error:`, err);
                killTree('onerror');
            };
            // LOCAL PATCH (2026-07-10, docs/oracle_mcp.md 追補5): stateless では
            // transport.onclose が正常完了時に発火せず、killTree が一度も呼ばれない
            // (実測: spawn 20 / reap 0、子チェーン ~20本・1.18GB が永久リーク)。
            // res 'close' は「応答送信完了 or クライアント切断」の両方で必ず発火する
            // ため、ここをライフサイクル終端として子プロセスグループを回収する。
            // OpenAI 側の background ジョブはサーバー側で継続し、oracle session
            // <slug> で回収可能（従来挙動と同じ）。
            res.on('close', () => killTree('res-close'));
            await transport.handleRequest(req, res, req.body);
        }
        catch (error) {
            logger.error('Error handling MCP request:', error);
            if (!res.headersSent) {
                res.status(500).json({
                    jsonrpc: '2.0',
                    error: {
                        code: -32603,
                        message: 'Internal server error',
                    },
                    id: null,
                });
            }
        }
    });
    app.get(streamableHttpPath, async (req, res) => {
        logger.info('Received GET MCP request');
        res.writeHead(405).end(JSON.stringify({
            jsonrpc: '2.0',
            error: {
                code: -32000,
                message: 'Method not allowed.',
            },
            id: null,
        }));
    });
    app.delete(streamableHttpPath, async (req, res) => {
        logger.info('Received DELETE MCP request');
        res.writeHead(405).end(JSON.stringify({
            jsonrpc: '2.0',
            error: {
                code: -32000,
                message: 'Method not allowed.',
            },
            id: null,
        }));
    });
    // LOCAL PATCH (2026-07-14, Phase 0B): supergateway 3.4.3 otherwise calls
    // listen(port) and exposes the unauthenticated Oracle MCP on every host
    // interface. This shared endpoint is local-only by contract.
    app.listen(port, '127.0.0.1', () => {
        logger.info(`Listening on 127.0.0.1:${port}`);
        logger.info(`StreamableHttp endpoint: http://127.0.0.1:${port}${streamableHttpPath}`);
    });
}
