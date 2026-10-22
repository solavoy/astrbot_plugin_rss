/* Plain JavaScript client for the authenticated AstrBot Plugin View bridge. */
const INTERVAL_ERROR = '抓取间隔需为非负整数，可带 d/h/m/s（天/时/分/秒），无单位按秒，0 表示继承全局';

function isPublicFeedUrl(value) {
    return /^https?:\/\//i.test(value);
}

function isRelativeFeedRoute(value) {
    return value.startsWith('/') && !value.startsWith('//') && !value.includes('\\') && !/[\u0000-\u001f\u007f]/.test(value);
}

function App() {
    return {
        ready: false,
        busy: false,
        title: 'RSS 订阅中心',
        timezone: 'Asia/Shanghai',
        route: '/',
        pageTitle: '总览',
        links: [
            { path: '/', href: '#/', label: '总览', icon: '▦' },
            { path: '/feeds', href: '#/feeds', label: '订阅源', icon: '◉' },
            { path: '/lists', href: '#/lists', label: '订阅列表', icon: '▤' },
            { path: '/deliveries', href: '#/deliveries', label: '消息记录', icon: '♧' },
            { path: '/data', href: '#/data', label: '数据管理', icon: '▣' }
        ],
        loading: false,
        error: '',
        notice: '',
        data: null,
        feeds: { items: [], total: 0, page: 1, page_size: 20 },
        lists: { items: [], total: 0, page: 1, page_size: 20 },
        deliveries: { items: [], total: 0, page: 1, page_size: 20 },
        targets: [],
        status: null,
        scope: 'all',
        query: '',
        createUrl: '',
        createTranslationEnabled: false,
        targetId: '',
        listName: '',
        listDescription: '',
        listInterval: '0',
        deliveryStatus: '',
        detail: null,
        detailTab: 'subscribers',
        preview: null,
        pageNo: 1,
        deliveryFilters: { platform: '', feed_id: '', list_id: '', target_id: '', date_from: '', date_to: '' },
        feedEnabled: true,
        feedTranslationEnabled: false,
        feedInterval: '0',
        editListName: '',
        editListDescription: '',
        editListInterval: '0',
        editListEnabled: true,
        selectedFeed: '',
        addValue: '',
        feedChoices: [],
        cleanups: [
            { value: 'articles', label: '清理已处理文章' }, { value: 'history', label: '清理投递历史' }, { value: 'cookies', label: '清理过期 Cookie' }, { value: 'media', label: '清理媒体缓存' }
        ],
        bridge: null,
        async start() {
            this.routeFromHash();
            window.addEventListener('hashchange', () => { this.routeFromHash(); this.load(); });
            this.bridge = window.AstrBotPluginView || null;
            if (!this.bridge) {
                this.error = 'AstrBot 管理面板桥接尚未就绪。请从 AstrBot 插件管理页打开此界面。';
                return;
            }
            try {
                let ctx = await this.bridge.ready();
                this.title = ctx?.displayName || this.title;
                this.theme(!!ctx?.isDark);
                this.ready = true;
                this.bridge.onContext?.(c => this.theme(!!c.isDark));
                await this.load();
            }
            catch (e) {
                this.error = this.err(e);
            }
        },
        theme(dark) { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; },
        routeFromHash() {
            let p = location.hash.replace(/^#/, '') || '/';
            this.route = p;
            this.detail = null;
            this.detailTab = 'subscribers';
            this.preview = null;
            this.selectedFeed = '';
            this.feedTranslationEnabled = false;
            this.addValue = '';
            this.targetId = '';
            this.error = '';
            this.notice = '';
            this.pageNo = 1;
            let root = p.split('/')[1] || '';
            this.pageTitle = ({ feeds: '订阅源', lists: '订阅列表', deliveries: '消息记录', data: '数据管理' })[root] || '总览';
            if (p.split('/').length > 2)
                this.pageTitle += ' · 详情';
        },
        go(path) { location.hash = '#' + path; },
        routeId() {
            const encodedId = this.route.split('/')[2];
            return encodedId ? decodeURIComponent(encodedId) : '';
        },
        detailEndpoint(resource, suffix = '') {
            return `${resource}/${encodeURIComponent(this.routeId())}${suffix}`;
        },
        parseInterval(value) {
            if (typeof value !== 'string' && typeof value !== 'number')
                return undefined;
            const text = String(value).trim();
            if (text === '')
                return null;
            const match = /^([0-9]+)[ \t\r\n\f\v]*([dhms]?)$/i.exec(text);
            if (!match)
                return undefined;
            return /^0+$/.test(match[1]) ? null : text;
        },
        async get(ep, params) {
            return await this.bridge.apiGet(ep, params || {});
        },
        async post(ep, body) {
            return await this.bridge.apiPost(ep, body || {});
        },
        async load() {
            if (!this.ready || !this.bridge)
                return;
            let seq = this.requestSeq = (this.requestSeq || 0) + 1;
            this.loading = true;
            this.error = '';
            try {
                let root = this.route.split('/')[1] || '', id = this.routeId();
                if (id)
                    id = encodeURIComponent(id);
                let result;
                if (root === '')
                    result = await this.get('dashboard');
                else if (root === 'feeds') {
                    let tg = await this.get('targets');
                    if (id) {
                        let d = await this.get('feeds/' + id);
                        if (seq === this.requestSeq) {
                            this.targets = tg;
                            this.detail = d;
                            this.setTimezone(d);
                            this.feedEnabled = !!d.feed?.enabled;
                            this.feedTranslationEnabled = !!d.feed?.translation_enabled;
                            this.feedInterval = d.feed?.poll_interval == null || d.feed.poll_interval === 0 ? '0' : String(d.feed.poll_interval);
                        }
                    }
                    else {
                        let rows = await this.get('feeds', { q: this.query.trim(), page: this.pageNo, page_size: 20, scope: this.scope || 'all' });
                        if (seq === this.requestSeq) {
                            this.targets = tg;
                            this.feeds = rows;
                            this.setTimezone(rows);
                        }
                    }
                }
                else if (root === 'lists') {
                    let tg = await this.get('targets');
                    if (id) {
                        let d = await this.get('lists/' + id), choices = await this.get('feeds', { scope: 'all', page: 1, page_size: 100 });
                        if (seq === this.requestSeq) {
                            this.targets = tg;
                            this.detail = d;
                            this.setTimezone(d);
                            this.editListName = d.list?.name || '';
                            this.editListDescription = d.list?.description || '';
                            this.editListInterval = d.list?.poll_interval == null || d.list.poll_interval === 0 ? '0' : String(d.list.poll_interval);
                            this.editListEnabled = !!d.list?.enabled;
                            this.feedChoices = choices.items || [];
                        }
                    }
                    else {
                        let rows = await this.get('lists', { q: this.query.trim(), page: this.pageNo, page_size: 20 });
                        if (seq === this.requestSeq) {
                            this.targets = tg;
                            this.lists = rows;
                            this.setTimezone(rows);
                        }
                    }
                }
                else if (root === 'deliveries') {
                    if (id)
                        result = await this.get('deliveries/' + id);
                    else {
                        let params = { page: this.pageNo, page_size: 20, ...this.deliveryFilters };
                        if (this.deliveryStatus)
                            params.status = this.deliveryStatus;
                        Object.keys(params).forEach(k => {
                            if (!params[k])
                                delete params[k];
                        });
                        result = await this.get('deliveries', params);
                    }
                }
                else if (root === 'data')
                    result = await this.get('data/status');
                if (seq === this.requestSeq) {
                    this.setTimezone(result);
                    if (root === '')
                        this.data = result;
                    else if (root === 'deliveries') {
                        if (id)
                            this.detail = result;
                        else
                            this.deliveries = result;
                    }
                    else if (root === 'data')
                        this.status = result;
                }
            }
            catch (e) {
                if (seq === this.requestSeq)
                    this.error = this.err(e);
            }
            finally {
                if (seq === this.requestSeq)
                    this.loading = false;
            }
        },
        setTimezone(payload) {
            let timezone = payload?.timezone;
            if (typeof timezone !== 'string')
                timezone = payload?.config?.general?.timezone;
            if (typeof timezone === 'string')
                this.timezone = timezone;
        },
        err(e) { return e?.message || String(e || '操作失败'); }, txt(v) { return v == null ? '—' : String(v); }, n(v) { return Number(v || 0).toLocaleString('zh-CN'); }, date(v) {
            if (v == null || v === '')
                return '—';
            let d = typeof v === 'number' ? new Date(v * 1000) : new Date(v);
            if (Number.isNaN(d.valueOf()))
                return '—';
            try {
                const parts = new Intl.DateTimeFormat('zh-CN', {
                    timeZone: this.timezone,
                    calendar: 'gregory',
                    numberingSystem: 'latn',
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    hourCycle: 'h23',
                }).formatToParts(d).reduce((values, part) => {
                    if (part.type !== 'literal')
                        values[part.type] = part.value;
                    return values;
                }, {});
                return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
            }
            catch {
                return `无法按时区“${this.timezone}”显示时间`;
            }
        }, bytes(v) { let n = Number(v || 0); return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }, safeUrl(v) {
            try {
                let u = new URL(v);
                return ['http:', 'https:'].includes(u.protocol) ? u.href : '#';
            }
            catch {
                return '#';
            }
        },
        entityPublicId(entity) {
            if (!entity)
                return '—';
            return String(entity.public_id || entity.feed_public_id || entity.list_public_id || '—');
        },
        openFeed(feed) {
            if (!feed?.public_id) {
                this.error = '订阅源缺少公开编号';
                return;
            }
            this.go('/feeds/' + encodeURIComponent(feed.public_id));
        },
        openList(list) {
            if (!list?.public_id) {
                this.error = '订阅列表缺少公开编号';
                return;
            }
            this.go('/lists/' + encodeURIComponent(list.public_id));
        },
        feedSubscribers() {
            if (Array.isArray(this.detail?.subscribers))
                return this.detail?.subscribers || [];
            const direct = (this.detail?.direct_subscribers || []).map(s => ({ ...s, paths: s.paths || ['直接订阅'] }));
            const inherited = (this.detail?.list_refs || []).map(l => ({
                target_id: l.target_id,
                label: l.target_label || l.label || l.name || (l.list_public_id ? `订阅列表 ${l.list_public_id}` : '订阅列表'),
                platform: l.platform,
                enabled: l.enabled,
                paths: l.paths || [`来自列表：${l.name || (l.list_public_id ? `编号 ${l.list_public_id}` : '订阅列表')}`],
            }));
            return [...direct, ...inherited];
        },
        subscriberPaths(subscriber) {
            const paths = Array.isArray(subscriber?.paths) ? subscriber.paths : [];
            return paths.map(path => {
                if (typeof path === 'string')
                    return path;
                let label;
                if (path.label || path.list_name || path.name || path.path)
                    label = path.label || path.list_name || path.name || path.path;
                else if (path.kind === 'direct' || path.kind === 'feed')
                    label = '直接订阅';
                else
                    label = path.list_public_id ? `列表 ${path.list_public_id}` : path.kind || '订阅路径';
                return path.enabled === false ? `${label}（已停用）` : label;
            });
        },
        subscriberTitle(subscriber) {
            return subscriber?.label || subscriber?.target_label || subscriber?.unified_origin || subscriber?.target_id || '目标';
        },
        subscriberPlatform(subscriber) {
            const instance = subscriber?.platform_instance;
            const platform = subscriber?.platform;
            return [instance, platform].filter(Boolean).join(' · ') || '平台';
        },
        subscriberHasOrigin(subscriber) {
            return !!(subscriber?.unified_origin && (subscriber?.label || subscriber?.target_label));
        },
        articleSource(article) {
            if (article?.feed_title)
                return `来源：${article.feed_title}`;
            return article?.feed_public_id ? `来源：源 ${article.feed_public_id}` : '';
        },
        articleDate(article) {
            const value = article?.published_at != null ? article.published_at : article?.updated_at != null ? article.updated_at : article?.first_seen_at;
            return this.date(value);
        },
        displaySourcePath(path) {
            const value = String(path || '');
            if (value.startsWith('list:'))
                return '订阅列表';
            if (value.startsWith('direct:'))
                return '直接订阅';
            return value || '—';
        },
        async scheduler(action) { await this.perform(async () => { await this.post('scheduler', { action }); this.notice = action === 'pause' ? '调度已暂停' : action === 'resume' ? '调度已恢复' : '已请求立即检查'; await this.load(); }); },
        async perform(fn) {
            if (this.busy)
                return;
            this.busy = true;
            this.error = '';
            try {
                await fn();
            }
            catch (e) {
                this.error = this.err(e);
            }
            finally {
                this.busy = false;
            }
        },
        async createFeed() {
            let reference = this.createUrl.trim();
            if (!isPublicFeedUrl(reference) && !isRelativeFeedRoute(reference)) {
                this.error = '请输入公开 HTTP(S) 地址或以单个 / 开头的 RSSHub 相对路径';
                return;
            }
            if (!this.targetId) {
                this.error = '请选择要订阅的已登记目标';
                return;
            }
            await this.perform(async () => {
                let r = await this.post('feeds/create', { url: reference, target_id: this.targetId, translation_enabled: this.createTranslationEnabled });
                this.createUrl = '';
                this.createTranslationEnabled = false;
                this.notice = '订阅源已添加并开始建立基线';
                await this.load();
                if (r?.feed?.public_id)
                    this.go('/feeds/' + encodeURIComponent(r.feed.public_id));
            });
        },
        async createList() {
            this.notice = '';
            if (!this.listName.trim()) {
                this.error = '请填写列表名称';
                return;
            }
            let interval = this.parseInterval(this.listInterval);
            if (interval === undefined) {
                this.error = INTERVAL_ERROR;
                return;
            }
            await this.perform(async () => {
                let r = await this.post('lists/create', { name: this.listName.trim(), description: this.listDescription.trim() || null, poll_interval: interval });
                this.notice = '订阅列表已创建';
                this.listName = '';
                this.listDescription = '';
                this.listInterval = '0';
                await this.load();
                if (r?.list?.public_id || r?.public_id)
                    this.go('/lists/' + encodeURIComponent(r.list?.public_id || r.public_id));
            });
        },
        async retry(id) { await this.perform(async () => { await this.post('deliveries/' + encodeURIComponent(id) + '/retry', {}); this.notice = '已将失败投递加入重试队列。'; await this.load(); }); },
        async resolve(id, resolution) {
            let prompt = resolution === 'delivered' ? '请先在目标平台确认消息确已送达，再确认标记为已送达。' : '确认人工核对后重新投递此结果未知的消息？';
            if (!window.confirm(prompt))
                return;
            await this.perform(async () => { await this.post('deliveries/' + encodeURIComponent(id) + '/resolve', { confirm: true, resolution }); this.notice = resolution === 'delivered' ? '已记录人工确认。' : '已安排人工确认后的重试。'; await this.load(); });
        },
        async feedAction(action) {
            if (action === 'test' && !this.targetId) {
                this.error = '请选择测试推送目标';
                return;
            }
            await this.perform(async () => {
                let body = action === 'test' ? { target_id: this.targetId } : action === 'preview' && this.targetId ? { target_id: this.targetId } : {};
                let r = await this.post(this.detailEndpoint('feeds', `/${action}`), body);
                if (action === 'refresh') {
                    if (r.status === 'skipped' && r.reason === 'concurrent_refresh')
                        this.notice = '订阅源刚刚已完成检查，本次刷新已合并，请查看最新抓取日志';
                    else
                        this.notice = r.status === 'not_modified' ? '检查完成，内容没有变化' : `检查完成，新增 ${r.new_articles || 0} 篇文章`;
                }
                else if (action === 'test')
                    this.notice = `测试推送状态：${r.status || '未知'}${r.operation_id ? '（操作 ID：' + r.operation_id + '）' : ''}`;
                else
                    this.preview = r;
                this.setTimezone(r);
                await this.load();
            });
        },
        async saveFeed() {
            this.notice = '';
            let v = this.parseInterval(this.feedInterval);
            if (v === undefined) {
                this.error = INTERVAL_ERROR;
                return;
            }
            await this.perform(async () => { await this.post(this.detailEndpoint('feeds', '/update'), { enabled: this.feedEnabled, poll_interval: v, translation_enabled: this.feedTranslationEnabled }); this.notice = '订阅源设置已保存'; await this.load(); });
        },
        async unsubscribe() {
            if (!this.targetId) {
                this.error = '请选择要取消的订阅目标';
                return;
            }
            if (!window.confirm('确认只取消这个目标的直接订阅关系？'))
                return;
            await this.perform(async () => { await this.post(this.detailEndpoint('feeds', '/unsubscribe'), { target_id: this.targetId }); this.notice = '已取消该目标订阅'; await this.load(); });
        },
        hasSession() { let s = this.detail?.cookie_status; return !!(s?.session_id && s?.validation_status && s.validation_status !== 'missing'); },
        async sessionAction(session, action) {
            await this.updateSession(session, action, {
                missingMessage: '当前来源没有可操作的会话 ID',
                clearPrompt: '确认清除该站点 Cookie 和浏览器会话？',
                refreshPrompt: '确认重新建立该站点访问会话？',
                clearNotice: '会话已清理',
                refreshNotice: '已请求刷新会话',
            });
        },
        async sessionStatusAction(session, action) {
            await this.updateSession(session, action, {
                missingMessage: '会话状态缺少操作所需的 ID 或来源',
                clearPrompt: `确认清理 ${session?.origin || session?.session_id || '此来源'} 的 Cookie 会话？`,
                clearNotice: 'Cookie 会话已清理',
                refreshNotice: '已请求刷新 Cookie 会话',
            });
        },
        async updateSession(session, action, copy) {
            let s = session || {}, id = String(s.session_id || s.id || '');
            if (!id) {
                this.error = copy.missingMessage;
                return;
            }
            let prompt = action === 'clear' ? copy.clearPrompt : copy.refreshPrompt;
            if (prompt && !window.confirm(prompt))
                return;
            await this.perform(async () => {
                await this.post(`sessions/${encodeURIComponent(id)}/${action}`, { confirm: true, origin: String(s.origin || '') });
                this.notice = action === 'clear' ? copy.clearNotice : copy.refreshNotice;
                await this.load();
            });
        },
        async saveList() {
            this.notice = '';
            if (!this.editListName.trim()) {
                this.error = '列表名称不能为空';
                return;
            }
            let interval = this.parseInterval(this.editListInterval);
            if (interval === undefined) {
                this.error = INTERVAL_ERROR;
                return;
            }
            await this.perform(async () => { await this.post(this.detailEndpoint('lists', '/update'), { name: this.editListName.trim(), description: this.editListDescription.trim(), enabled: !!this.editListEnabled, poll_interval: interval }); this.notice = '列表设置已保存'; await this.load(); });
        },
        async deleteList() {
            if (!window.confirm('确认删除这个列表？全局 Feed 和直接订阅关系会保留。'))
                return;
            await this.perform(async () => { await this.post(this.detailEndpoint('lists', '/delete'), { confirm: true }); this.notice = '列表已删除'; this.go('/lists'); });
        },
        async addMember(value) {
            let clean = String(value || '').trim();
            if (!clean) {
                this.error = '请填写订阅源数字编号、公开 URL 或 RSSHub 相对路径';
                return;
            }
            let isUrl = isPublicFeedUrl(clean);
            let isRoute = isRelativeFeedRoute(clean);
            if (!isUrl && !isRoute && (!/^\d+$/.test(clean) || Number(clean) <= 0)) {
                this.error = '请输入正整数编号、公开 HTTP(S) URL 或有效的 RSSHub 相对路径';
                return;
            }
            await this.perform(async () => { await this.post(this.detailEndpoint('lists', '/members/add'), isUrl || isRoute ? { url: clean } : { feed_id: Number(clean) }); this.notice = '已添加 ' + clean; this.selectedFeed = ''; this.addValue = ''; await this.load(); });
        },
        async addMany() {
            let values = this.addValue.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
            if (!values.length) {
                this.error = '每行输入一个订阅源数字编号、公开 URL 或 RSSHub 相对路径';
                return;
            }
            const invalid = values.find(value => !isPublicFeedUrl(value) && !isRelativeFeedRoute(value) && (!/^\d+$/.test(value) || Number(value) <= 0));
            if (invalid) {
                this.error = `“${invalid}”不是有效的编号、HTTP(S) URL 或 RSSHub 相对路径`;
                return;
            }
            let done = 0;
            await this.perform(async () => {
                for (let value of values) {
                    await this.post(this.detailEndpoint('lists', '/members/add'), isPublicFeedUrl(value) || isRelativeFeedRoute(value) ? { url: value } : { feed_id: Number(value) });
                    done++;
                }
                this.notice = `已处理 ${done} 项列表成员`;
                this.addValue = '';
                await this.load();
            });
            if (done < values.length)
                this.error = this.error + `（已处理 ${done} 项）`;
        },
        async removeMember(feed_public_id) {
            if (!/^\d+$/.test(String(feed_public_id || '')) || Number(feed_public_id) <= 0) {
                this.error = '成员缺少有效的订阅源数字编号';
                return;
            }
            if (!window.confirm('确认从此列表移除该 Feed？全局 Feed 与其他订阅关系会保留。'))
                return;
            await this.perform(async () => { await this.post(this.detailEndpoint('lists', '/members/remove'), { feed_id: Number(feed_public_id) }); this.notice = '成员已移除'; await this.load(); });
        },
        async listSubscription(action) {
            if (!this.targetId) {
                this.error = '请选择已登记目标';
                return;
            }
            await this.perform(async () => { await this.post(this.detailEndpoint('lists', `/${action}`), { target_id: this.targetId }); this.notice = action === 'subscribe' ? '已订阅列表' : '已取消列表订阅'; await this.load(); });
        },
        async listPreview() { await this.perform(async () => { this.preview = await this.post(this.detailEndpoint('lists', '/preview'), this.targetId ? { target_id: this.targetId } : {}); this.setTimezone(this.preview); this.notice = '只生成预览，不会发送消息'; }); },
        previewItems(value) {
            const root = value && typeof value === 'object' ? value : {};
            const messages = Array.isArray(root.messages) ? root.messages : [];
            const rows = messages.map(message => {
                const payload = message.payload || {};
                const card = payload.card || {};
                const title = card.header?.title?.content || payload.text || '推送消息';
                const subtitle = card.header?.subtitle?.content || '';
                const body = [];
                const links = [];
                let originalUrl = '';
                if (message.message_type === 'media') {
                    body.push('视频（正文后单独发送）');
                    if (this.safeUrl(payload.source_url) !== '#')
                        links.push(payload.source_url);
                }
                if (message.message_type === 'video') {
                    body.push('视频');
                    if (this.safeUrl(payload.url) !== '#')
                        links.push(payload.url);
                }
                for (const part of payload.parts || []) {
                    if (part.text)
                        body.push(part.text);
                    if (this.safeUrl(part.url) !== '#')
                        links.push(part.url);
                }
                for (const element of card.body?.elements || []) {
                    if (element.tag === 'markdown' && element.content)
                        body.push(element.content);
                    if (element.text?.content)
                        body.push(element.text.content);
                    for (const behavior of element.behaviors || []) {
                        if (this.safeUrl(behavior.default_url) !== '#')
                            links.push(behavior.default_url);
                    }
                    if (element.tag === 'img' && element.img_key)
                        body.push(`[图片：${element.alt?.content || '图片'}]`);
                }
                let messageBody = body.join('\n').trim();
                const footer = messageBody.match(/(?:^|\n)原文地址：\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)\s*$/);
                if (footer && footer[1] === footer[2] && this.safeUrl(footer[2]) !== '#') {
                    originalUrl = footer[2];
                    messageBody = messageBody.slice(0, footer.index).trimEnd();
                }
                return {
                    title,
                    subtitle,
                    body: messageBody,
                    originalUrl,
                    links: [...new Set(links)],
                    articleIds: (message.article_ids || []).map(String),
                };
            });
            if (rows.length)
                return rows;
            const body = [];
            const links = [];
            const collect = block => {
                if (block?.text)
                    body.push(String(block.text));
                if (this.safeUrl(block?.url) !== '#')
                    links.push(block.url);
                if (Array.isArray(block?.children))
                    block.children.forEach(collect);
            };
            return (root.articles || []).map(article => {
                body.length = 0;
                links.length = 0;
                (article.content_blocks || []).forEach(collect);
                const originalUrl = this.safeUrl(article.canonical_url) !== '#' ? article.canonical_url : '';
                return {
                    title: article.title || '文章',
                    subtitle: '',
                    body: body.join('\n').trim(),
                    originalUrl,
                    links: [...new Set(links)],
                    articleIds: [article.article_id].filter(Boolean),
                };
            });
        },
        async download(endpoint, name) { await this.perform(async () => { await this.bridge.download(endpoint, {}, name); this.notice = '已开始下载 ' + name; }); },
        async cleanup(kind, label) {
            if (!window.confirm(`确认清理${label}？请确保相关数据已备份。`))
                return;
            await this.perform(async () => { let r = await this.post('data/cleanup', { confirm: true, kind }); this.notice = `${label}清理完成${r?.removed === undefined ? '' : `，移除 ${r.removed} 项`}`; await this.load(); });
        },
        async importFile(event) {
            let f = event.target.files?.[0];
            if (!f)
                return;
            if (!window.confirm(`导入 ${f.name}？后端会校验内容并事务处理。`)) {
                event.target.value = '';
                return;
            }
            await this.perform(async () => { await this.post('data/import/confirm', { confirm: true, filename: f.name, size: f.size }); let r = await this.bridge.upload('data/import', f); let skipped = Number(r?.skipped_unregistered_targets || 0); this.notice = skipped ? `数据导入完成；${skipped} 个未登记目标的关系已跳过。` : '数据导入完成'; await this.load(); });
            event.target.value = '';
        },
        async restoreFile(event) {
            let f = event.target.files?.[0];
            if (!f)
                return;
            if (!window.confirm(`将从 ${f.name} 恢复数据库。恢复前会先生成备份，当前操作可能替换现有数据。继续？`)) {
                event.target.value = '';
                return;
            }
            await this.perform(async () => { await this.post('data/restore/confirm', { confirm: true, filename: f.name, size: f.size }); await this.bridge.upload('data/restore', f); this.notice = '数据库恢复完成'; await this.load(); });
            event.target.value = '';
        },
    };
}
PetiteVue.createApp({ App }).mount('#app');
