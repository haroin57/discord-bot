import express from 'express';
import cors from 'cors';
import { spawn } from 'child_process';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { getVersion } from '../lib/getVersion.js';
import { onSignals } from '../lib/onSignals.js';
import { serializeCorsOrigin } from '../lib/serializeCorsOrigin.js';
import { randomUUID } from 'node:crypto';
import { isInitializeRequest } from '@modelcontextprotocol/sdk/types.js';
import { SessionAccessCounter } from '../lib/sessionAccessCounter.js';
const setResponseHeaders = ({ res, headers, }) => Object.entries(headers).forEach(([key, value]) => {
    res.setHeader(key, value);
});
export async function stdioToStatefulStreamableHttp(args) {
    const { stdioCmd, port, streamableHttpPath, logger, corsOrigin, healthEndpoints, headers, sessionTimeout, } = args;
    logger.info(`  - Headers: ${Object(headers).length ? JSON.stringify(headers) : '(none)'}`);
    logger.info(`  - port: ${port}`);
    logger.info(`  - stdio: ${stdioCmd}`);
    logger.info(`  - streamableHttpPath: ${streamableHttpPath}`);
    logger.info(`  - CORS: ${corsOrigin ? `enabled (${serializeCorsOrigin({ corsOrigin })})` : 'disabled'}`);
    logger.info(`  - Health endpoints: ${healthEndpoints.length ? healthEndpoints.join(', ') : '(none)'}`);
    logger.info(`  - Session timeout: ${sessionTimeout ? `${sessionTimeout}ms` : 'disabled'}`);
    onSignals({ logger });
    const app = express();
    // LOCAL PATCH (2026-07-10, docs/oracle_mcp.md 追補5): express.json() の
    // デフォルト 100kb 制限だと長い consult prompt が 413 で弾かれるため拡大。
    app.use(express.json({ limit: '8mb' }));
    if (corsOrigin) {
        app.use(cors({
            origin: corsOrigin,
            exposedHeaders: ['Mcp-Session-Id'],
        }));
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
    // Map to store transports by session ID
    const transports = {};
    // Session access counter for timeout management
    const sessionCounter = sessionTimeout
        ? new SessionAccessCounter(sessionTimeout, (sessionId) => {
            logger.info(`Session ${sessionId} timed out, cleaning up`);
            const transport = transports[sessionId];
            if (transport) {
                transport.close();
            }
            delete transports[sessionId];
        }, logger)
        : null;
    // Handle POST requests for client-to-server communication
    app.post(streamableHttpPath, async (req, res) => {
        // Check for existing session ID
        const sessionId = req.headers['mcp-session-id'];
        let transport;
        if (sessionId && transports[sessionId]) {
            // Reuse existing transport
            transport = transports[sessionId];
            // Increment session access count
            sessionCounter?.inc(sessionId, 'POST request for existing session');
        }
        else if (!sessionId && isInitializeRequest(req.body)) {
            // New initialization request
            const server = new Server({ name: 'supergateway', version: getVersion() }, { capabilities: {} });
            transport = new StreamableHTTPServerTransport({
                sessionIdGenerator: () => randomUUID(),
                onsessioninitialized: (sessionId) => {
                    // Store the transport by session ID
                    transports[sessionId] = transport;
                    // Initialize session access count
                    sessionCounter?.inc(sessionId, 'session initialization');
                },
            });
            await server.connect(transport);
            // LOCAL PATCH (2026-07-10, docs/oracle_mcp.md 追補5): ADR-0037 と同型の
            // 孫孤児化対策。shell:true は実 MCP を孫プロセスとして spawn するため、
            // 素の child.kill() はラッパー shell しか殺せず node 孫が PPid=1 に
            // 孤児化して積み上がる。detached:true でプロセスグループリーダー化し、
            // グループごと SIGTERM → 15s 後 SIGKILL で回収する。
            const child = spawn(stdioCmd, { shell: true, detached: true });
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
                        try {
                            // LOCAL PATCH (2026-07-10, docs/oracle_mcp.md 追補5):
                            // transport.send は async。クライアント切断後の応答で
                            // unhandled rejection になり node ごと死ぬため
                            // (stateless/SSE 側と同じパターン) rejection を捕捉する。
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
                child.stdin.write(JSON.stringify(msg) + '\n');
            };
            transport.onclose = () => {
                logger.info(`StreamableHttp connection closed (session ${sessionId})`);
                if (transport.sessionId) {
                    sessionCounter?.clear(transport.sessionId, false, 'transport being closed');
                    delete transports[transport.sessionId];
                }
                // LOCAL PATCH (2026-07-10): child.kill() では孫が残る → killTree で回収。
                killTree('onclose');
            };
            transport.onerror = (err) => {
                logger.error(`StreamableHttp error (session ${sessionId}):`, err);
                if (transport.sessionId) {
                    sessionCounter?.clear(transport.sessionId, false, 'transport emitting error');
                    delete transports[transport.sessionId];
                }
                // LOCAL PATCH (2026-07-10): child.kill() では孫が残る → killTree で回収。
                killTree('onerror');
            };
        }
        else {
            // Invalid request
            res.status(400).json({
                jsonrpc: '2.0',
                error: {
                    code: -32000,
                    message: 'Bad Request: No valid session ID provided',
                },
                id: null,
            });
            return;
        }
        // Decrement session access count when response ends
        let responseEnded = false;
        const handleResponseEnd = (event) => {
            if (!responseEnded && transport.sessionId) {
                responseEnded = true;
                logger.info(`Response ${event}`, transport.sessionId);
                sessionCounter?.dec(transport.sessionId, `POST response ${event}`);
            }
        };
        res.on('finish', () => handleResponseEnd('finished'));
        res.on('close', () => handleResponseEnd('closed'));
        // Handle the request
        await transport.handleRequest(req, res, req.body);
    });
    // Reusable handler for GET and DELETE requests
    const handleSessionRequest = async (req, res) => {
        const sessionId = req.headers['mcp-session-id'];
        if (!sessionId || !transports[sessionId]) {
            res.status(400).send('Invalid or missing session ID');
            return;
        }
        // Increment session access count
        sessionCounter?.inc(sessionId, `${req.method} request for existing session`);
        // Decrement session access count when response ends
        let responseEnded = false;
        const handleResponseEnd = (event) => {
            if (!responseEnded) {
                responseEnded = true;
                logger.info(`Response ${event}`, sessionId);
                sessionCounter?.dec(sessionId, `${req.method} response ${event}`);
            }
        };
        res.on('finish', () => handleResponseEnd('finished'));
        res.on('close', () => handleResponseEnd('closed'));
        const transport = transports[sessionId];
        await transport.handleRequest(req, res);
    };
    // Handle GET requests for server-to-client notifications via SSE
    app.get(streamableHttpPath, handleSessionRequest);
    // Handle DELETE requests for session termination
    app.delete(streamableHttpPath, handleSessionRequest);
    app.listen(port, () => {
        logger.info(`Listening on port ${port}`);
        logger.info(`StreamableHttp endpoint: http://localhost:${port}${streamableHttpPath}`);
    });
}
