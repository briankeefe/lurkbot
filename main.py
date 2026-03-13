#!/usr/bin/env python3
"""
LurkBot - Reddit Subreddit Scanner for Marketing Signals
A FastAPI app that scans Reddit for organic marketing opportunities.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database setup
DATABASE_PATH = "./data/lurkbot.db"

def init_database():
    """Initialize SQLite database with required tables."""
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                subreddit TEXT,
                title TEXT,
                selftext TEXT,
                url TEXT,
                score INTEGER,
                num_comments INTEGER,
                created_utc INTEGER,
                signal_types TEXT,
                keywords_matched TEXT,
                relevance_score REAL,
                seen BOOLEAN DEFAULT FALSE,
                scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_relevance_score 
            ON threads(relevance_score DESC, scraped_at DESC)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_seen_relevance 
            ON threads(seen, relevance_score DESC)
        """)
        
        conn.commit()
        logger.info("Database initialized")

# Load configuration
def load_config():
    """Load configuration from config.json."""
    try:
        with open("config.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("config.json not found")
        raise
    except json.JSONDecodeError:
        logger.error("Invalid JSON in config.json")
        raise

config = load_config()

# Reddit API client with rate limiting
class RedditScanner:
    """Reddit JSON API scanner with rate limiting and error handling."""
    
    def __init__(self):
        self.last_request_time = 0
        self.request_delay = 1.5  # 1.5s between requests (production tested)
        self.cooldown_until = 0
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'linux:com.lurkbot.scanner:v1.0 (by /u/lurkbot_user)'
        })
    
    def _wait_for_rate_limit(self):
        """Enforce rate limiting between requests."""
        # Check cooldown first
        if time.time() < self.cooldown_until:
            wait_time = self.cooldown_until - time.time()
            logger.info(f"In cooldown, waiting {wait_time:.1f}s")
            time.sleep(wait_time)
            self.cooldown_until = 0
        
        # Enforce minimum delay between requests
        elapsed = time.time() - self.last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        
        self.last_request_time = time.time()
    
    def _handle_error(self, response, url):
        """Handle Reddit API errors with appropriate cooldowns."""
        if response.status_code == 429:
            # Rate limited - check for reset header
            reset_header = response.headers.get('x-ratelimit-reset')
            if reset_header:
                cooldown = float(reset_header) + 10  # Add 10s buffer
            else:
                cooldown = 300 + (time.time() % 300)  # 5-10 min fallback
            
            self.cooldown_until = time.time() + cooldown
            logger.warning(f"Rate limited, cooldown for {cooldown:.1f}s")
            raise Exception(f"Rate limited, retry after {cooldown:.1f}s")
        
        elif response.status_code == 403:
            # IP blocked - longer cooldown
            cooldown = 600 + (time.time() % 300)  # 10-15 min
            self.cooldown_until = time.time() + cooldown
            logger.warning(f"IP blocked (403), cooldown for {cooldown:.1f}s")
            raise Exception(f"Blocked by Reddit, retry after {cooldown:.1f}s")
        
        elif response.status_code >= 500:
            # Reddit server error - short retry
            logger.warning(f"Reddit server error {response.status_code}")
            raise Exception(f"Reddit server error {response.status_code}")
        
        else:
            response.raise_for_status()
    
    def scan_subreddit(self, subreddit: str, limit: int = 100) -> Dict:
        """Scan a subreddit for new posts."""
        self._wait_for_rate_limit()
        
        url = f"https://www.reddit.com/r/{subreddit}/new.json"
        params = {
            'limit': min(limit, 100),  # Reddit max is 100
            'raw_json': 1
        }
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            
            if response.status_code != 200:
                self._handle_error(response, url)
            
            data = response.json()
            posts = []
            
            if 'data' in data and 'children' in data['data']:
                for child in data['data']['children']:
                    if child['kind'] == 't3':  # Post
                        post_data = child['data']
                        posts.append({
                            'id': post_data['id'],
                            'title': post_data['title'],
                            'selftext': post_data.get('selftext', ''),
                            'url': post_data['url'],
                            'score': post_data['score'],
                            'num_comments': post_data['num_comments'],
                            'created_utc': post_data['created_utc'],
                            'author': post_data.get('author', '[deleted]')
                        })
            
            logger.info(f"Scanned r/{subreddit}: {len(posts)} posts")
            return {
                'subreddit': subreddit,
                'posts': posts,
                'success': True
            }
        
        except Exception as e:
            logger.error(f"Error scanning r/{subreddit}: {e}")
            return {
                'subreddit': subreddit,
                'posts': [],
                'success': False,
                'error': str(e)
            }
    
    def search_subreddit(self, subreddit: str, query: str, limit: int = 100) -> Dict:
        """Search a subreddit for specific keywords."""
        self._wait_for_rate_limit()
        
        url = f"https://www.reddit.com/r/{subreddit}/search.json"
        params = {
            'q': query,
            'restrict_sr': 'true',
            'sort': 'new',
            'limit': min(limit, 100),
            'raw_json': 1
        }
        
        try:
            response = self.session.get(url, params=params, timeout=30)
            
            if response.status_code != 200:
                self._handle_error(response, url)
            
            data = response.json()
            posts = []
            
            if 'data' in data and 'children' in data['data']:
                for child in data['data']['children']:
                    if child['kind'] == 't3':
                        post_data = child['data']
                        posts.append({
                            'id': post_data['id'],
                            'title': post_data['title'],
                            'selftext': post_data.get('selftext', ''),
                            'url': post_data['url'],
                            'score': post_data['score'],
                            'num_comments': post_data['num_comments'],
                            'created_utc': post_data['created_utc'],
                            'author': post_data.get('author', '[deleted]')
                        })
            
            logger.info(f"Searched r/{subreddit} for '{query}': {len(posts)} posts")
            return {
                'subreddit': subreddit,
                'query': query,
                'posts': posts,
                'success': True
            }
        
        except Exception as e:
            logger.error(f"Error searching r/{subreddit} for '{query}': {e}")
            return {
                'subreddit': subreddit,
                'query': query,
                'posts': [],
                'success': False,
                'error': str(e)
            }

# Signal pattern matching
def match_signals(text: str) -> Dict:
    """Match text against signal patterns and return matches."""
    text_lower = text.lower()
    matched_signals = []
    matched_patterns = []
    
    for category, patterns in config['signal_patterns'].items():
        for pattern in patterns:
            if pattern.lower() in text_lower:
                matched_signals.append(category)
                matched_patterns.append(pattern)
    
    return {
        'signal_types': list(set(matched_signals)),
        'matched_patterns': matched_patterns
    }

def match_keywords(text: str) -> List[str]:
    """Match text against configured keywords."""
    text_lower = text.lower()
    matched = []
    
    for keyword in config['keywords']:
        if keyword.lower() in text_lower:
            matched.append(keyword)
    
    return matched

def calculate_relevance_score(post: Dict, signal_types: List[str], keywords: List[str]) -> float:
    """Calculate relevance score for a post."""
    score = 0.0
    
    # Signal matches (highest weight)
    score += len(signal_types) * 2.0
    
    # Keyword matches  
    score += len(keywords) * 1.0
    
    # Reddit engagement (normalized)
    score += post['score'] / 100.0
    score += post['num_comments'] / 10.0
    
    return round(score, 2)

# Background scanning
reddit_scanner = RedditScanner()
scheduler = AsyncIOScheduler()

async def scan_all_subreddits():
    """Background task to scan all configured subreddits."""
    logger.info("Starting scheduled scan of all subreddits")
    
    for subreddit in config['subreddits']:
        try:
            # Scan recent posts
            result = reddit_scanner.scan_subreddit(subreddit, limit=100)
            
            if not result['success']:
                logger.error(f"Failed to scan r/{subreddit}: {result.get('error')}")
                continue
            
            # Also search for keywords in this subreddit
            for keyword in config['keywords']:
                search_result = reddit_scanner.search_subreddit(subreddit, keyword, limit=50)
                if search_result['success']:
                    result['posts'].extend(search_result['posts'])
            
            seen_ids = set()
            unique_posts = []
            for post in result['posts']:
                if post['id'] not in seen_ids:
                    seen_ids.add(post['id'])
                    unique_posts.append(post)
            
            # Process and store posts
            with sqlite3.connect(DATABASE_PATH) as conn:
                for post in unique_posts:
                    # Check if already exists
                    existing = conn.execute(
                        "SELECT id FROM threads WHERE id = ?", (post['id'],)
                    ).fetchone()
                    
                    if existing:
                        continue
                    
                    # Match signals and keywords
                    full_text = f"{post['title']} {post['selftext']}"
                    signal_match = match_signals(full_text)
                    keyword_match = match_keywords(full_text)
                    
                    # Only store posts with matches
                    if signal_match['signal_types'] or keyword_match:
                        relevance_score = calculate_relevance_score(
                            post, signal_match['signal_types'], keyword_match
                        )
                        
                        conn.execute("""
                            INSERT INTO threads 
                            (id, subreddit, title, selftext, url, score, num_comments, 
                             created_utc, signal_types, keywords_matched, relevance_score)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            post['id'],
                            subreddit,
                            post['title'],
                            post['selftext'],
                            post['url'],
                            post['score'],
                            post['num_comments'],
                            int(post['created_utc']),
                            json.dumps(signal_match['signal_types']),
                            json.dumps(keyword_match),
                            relevance_score
                        ))
                
                conn.commit()
            
            logger.info(f"Processed r/{subreddit}")
            
        except Exception as e:
            logger.error(f"Error processing r/{subreddit}: {e}")
    
    logger.info("Completed scheduled scan")

# FastAPI app setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app lifecycle."""
    # Startup
    init_database()
    scheduler.add_job(
        scan_all_subreddits,
        'interval',
        hours=config['scan_interval_hours'],
        id='scan_job',
        name='Scan all subreddits'
    )
    scheduler.start()
    logger.info("Scheduler started")
    yield
    # Shutdown
    scheduler.shutdown(wait=True)
    logger.info("Scheduler stopped")

app = FastAPI(
    title="LurkBot",
    description="Reddit subreddit scanner for marketing signals",
    lifespan=lifespan
)

# Models
class ThreadResponse(BaseModel):
    id: str
    subreddit: str
    title: str
    url: str
    score: int
    num_comments: int
    signal_types: List[str]
    keywords_matched: List[str]
    relevance_score: float
    seen: bool
    created_utc: int
    scraped_at: str

# API endpoints
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Main dashboard HTML."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LurkBot - Reddit Signal Feed</title>
    <style>
        *, *::before, *::after { box-sizing: border-box; }
        
        body {
            font-family: system-ui, sans-serif;
            font-size: 1rem;
            line-height: 1.5;
            max-width: 1200px;
            margin: 0 auto;
            padding: 1rem;
            color: #1a1a1a;
            background: #f9f9f9;
        }
        
        header {
            border-bottom: 2px solid #e0e0e0;
            margin-bottom: 2rem;
            padding-bottom: 1rem;
        }
        
        h1 { color: #2c3e50; margin: 0; }
        h1 small { color: #666; font-weight: normal; }
        
        .controls {
            margin-bottom: 2rem;
            display: flex;
            gap: 1rem;
            align-items: center;
            flex-wrap: wrap;
        }
        
        button {
            padding: 0.5rem 1rem;
            border: 1px solid #ddd;
            border-radius: 4px;
            background: white;
            cursor: pointer;
            font-size: 0.9rem;
        }
        
        button:hover { background: #f5f5f5; }
        button.primary { background: #3498db; color: white; border-color: #3498db; }
        button.primary:hover { background: #2980b9; }
        
        .feed {
            list-style: none;
            margin: 0;
            padding: 0;
        }
        
        .post {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 1rem;
            padding: 1rem;
            margin-bottom: 0.5rem;
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            transition: opacity 0.2s;
        }
        
        .post.seen {
            opacity: 0.45;
        }
        
        .post-main {
            flex: 1;
            min-width: 0;
        }
        
        .post-title {
            display: block;
            font-size: 0.95rem;
            font-weight: 600;
            color: #2c3e50;
            text-decoration: none;
            margin-bottom: 0.5rem;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
        }
        
        .post-title:hover { text-decoration: underline; }
        .post.seen .post-title { color: #888; }
        
        .post-meta {
            font-size: 0.78rem;
            color: #666;
            margin-bottom: 0.5rem;
        }
        
        .meta-item + .meta-item::before { content: " · "; }
        
        .post-stats {
            display: flex;
            align-items: flex-start;
            gap: 0.4rem;
            flex-shrink: 0;
            flex-wrap: wrap;
        }
        
        .badge {
            display: inline-block;
            font-size: 0.7rem;
            font-weight: 600;
            padding: 0.15em 0.5em;
            border-radius: 10em;
            white-space: nowrap;
            line-height: 1.4;
        }
        
        .badge-score { background: #e8f5e9; color: #2e7d32; }
        .badge-comments { background: #e3f2fd; color: #1565c0; }
        .badge-relevance { background: #fff3e0; color: #e65100; }
        .badge-signal { background: #fce4ec; color: #b71c1c; }
        .badge-keyword { background: #f3e5f5; color: #6a1b9a; }
        
        .btn-seen {
            font-size: 0.7rem;
            padding: 0.2em 0.6em;
            border: 1px solid #ccc;
            border-radius: 4px;
            background: transparent;
            cursor: pointer;
            color: #555;
        }
        
        .btn-seen:hover { background: #eee; }
        .post.seen .btn-seen { 
            border-color: #bbb; 
            color: #aaa; 
            cursor: default;
        }
        
        .loading { 
            text-align: center; 
            padding: 2rem; 
            color: #666; 
        }
        
        .error { 
            background: #ffebee; 
            color: #c62828; 
            padding: 1rem; 
            border-radius: 4px; 
            margin-bottom: 1rem; 
        }
        
        @media (max-width: 600px) {
            .post {
                flex-direction: column;
                gap: 0.4rem;
            }
            
            .post-stats {
                justify-content: flex-start;
            }
            
            .controls {
                flex-direction: column;
                align-items: stretch;
            }
        }
    </style>
</head>
<body>
    <header>
        <h1>LurkBot <small>Reddit Signal Feed</small></h1>
    </header>
    
    <div class="controls">
        <button class="primary" onclick="triggerScan()">Trigger Scan</button>
        <button onclick="refreshFeed()">Refresh Feed</button>
        <button onclick="showOnlyUnseen()">Show Unseen Only</button>
        <button onclick="showAll()">Show All</button>
    </div>
    
    <ul class="feed" id="feed">
        <li class="loading">Loading posts...</li>
    </ul>

    <script>
        let showUnseenOnly = false;
        let feedData = [];
        
        async function loadFeed() {
            try {
                const response = await fetch('/api/feed');
                if (!response.ok) throw new Error('Failed to load feed');
                
                feedData = await response.json();
                renderFeed();
            } catch (error) {
                document.getElementById('feed').innerHTML = 
                    '<li class="error">Error loading feed: ' + error.message + '</li>';
            }
        }
        
        function renderFeed() {
            const feed = document.getElementById('feed');
            const filteredData = showUnseenOnly ? feedData.filter(p => !p.seen) : feedData;
            
            if (filteredData.length === 0) {
                feed.innerHTML = '<li class="loading">No posts found</li>';
                return;
            }
            
            feed.innerHTML = filteredData.map(post => {
                const createdDate = new Date(post.created_utc * 1000);
                const scrapedDate = new Date(post.scraped_at);
                
                return `
                    <li class="post ${post.seen ? 'seen' : ''}" data-id="${post.id}">
                        <div class="post-main">
                            <a class="post-title" href="${post.url}" target="_blank">${post.title}</a>
                            <div class="post-meta">
                                <span class="meta-item">r/${post.subreddit}</span>
                                <span class="meta-item">${timeAgo(createdDate)}</span>
                                <span class="meta-item">scraped ${timeAgo(scrapedDate)}</span>
                            </div>
                        </div>
                        <div class="post-stats">
                            <span class="badge badge-relevance">score: ${post.relevance_score}</span>
                            <span class="badge badge-score">↑ ${post.score}</span>
                            <span class="badge badge-comments">💬 ${post.num_comments}</span>
                            ${post.signal_types.map(s => `<span class="badge badge-signal">${s}</span>`).join('')}
                            ${post.keywords_matched.map(k => `<span class="badge badge-keyword">${k}</span>`).join('')}
                            <button class="btn-seen" onclick="markSeen('${post.id}')" 
                                ${post.seen ? 'disabled' : ''}>
                                ${post.seen ? 'seen ✓' : 'mark seen'}
                            </button>
                        </div>
                    </li>
                `;
            }).join('');
        }
        
        async function markSeen(postId) {
            try {
                const response = await fetch(`/mark-seen/${postId}`, { method: 'POST' });
                if (!response.ok) throw new Error('Failed to mark as seen');
                
                // Update local data
                const post = feedData.find(p => p.id === postId);
                if (post) {
                    post.seen = true;
                    renderFeed();
                }
            } catch (error) {
                alert('Error: ' + error.message);
            }
        }
        
        async function triggerScan() {
            try {
                const btn = event.target;
                btn.disabled = true;
                btn.textContent = 'Scanning...';
                
                const response = await fetch('/api/scan', { method: 'POST' });
                if (!response.ok) throw new Error('Scan failed');
                
                const result = await response.json();
                alert(`Scan completed: ${result.message}`);
                
                // Refresh feed after scan
                await loadFeed();
            } catch (error) {
                alert('Scan error: ' + error.message);
            } finally {
                const btn = event.target;
                btn.disabled = false;
                btn.textContent = 'Trigger Scan';
            }
        }
        
        function refreshFeed() {
            loadFeed();
        }
        
        function showOnlyUnseen() {
            showUnseenOnly = true;
            renderFeed();
        }
        
        function showAll() {
            showUnseenOnly = false;
            renderFeed();
        }
        
        function timeAgo(date) {
            const seconds = Math.floor((new Date() - date) / 1000);
            
            const intervals = {
                year: 31536000,
                month: 2592000,
                week: 604800,
                day: 86400,
                hour: 3600,
                minute: 60
            };
            
            for (const [unit, secondsInUnit] of Object.entries(intervals)) {
                const interval = Math.floor(seconds / secondsInUnit);
                if (interval >= 1) {
                    return interval === 1 ? `1 ${unit} ago` : `${interval} ${unit}s ago`;
                }
            }
            
            return 'just now';
        }
        
        // Load feed on page load
        loadFeed();
        
        // Auto-refresh every 30 seconds
        setInterval(loadFeed, 30000);
    </script>
</body>
</html>
    """

@app.get("/api/feed")
async def get_feed() -> List[ThreadResponse]:
    """Get feed of Reddit posts with signals."""
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute("""
            SELECT * FROM threads 
            ORDER BY relevance_score DESC, scraped_at DESC 
            LIMIT 100
        """)
        
        threads = []
        for row in cursor.fetchall():
            threads.append(ThreadResponse(
                id=row['id'],
                subreddit=row['subreddit'],
                title=row['title'],
                url=row['url'],
                score=row['score'],
                num_comments=row['num_comments'],
                signal_types=json.loads(row['signal_types']) if row['signal_types'] else [],
                keywords_matched=json.loads(row['keywords_matched']) if row['keywords_matched'] else [],
                relevance_score=row['relevance_score'],
                seen=bool(row['seen']),
                created_utc=row['created_utc'],
                scraped_at=row['scraped_at']
            ))
        
        return threads

@app.post("/api/scan")
async def trigger_scan():
    """Manually trigger a scan of all subreddits."""
    try:
        await scan_all_subreddits()
        return {"message": "Scan completed successfully"}
    except Exception as e:
        logger.error(f"Manual scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/mark-seen/{thread_id}")
async def mark_thread_seen(thread_id: str):
    """Mark a thread as seen."""
    with sqlite3.connect(DATABASE_PATH) as conn:
        result = conn.execute(
            "UPDATE threads SET seen = TRUE WHERE id = ?", 
            (thread_id,)
        )
        
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Thread not found")
        
        conn.commit()
    
    return {"message": "Thread marked as seen"}

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "scheduler_running": scheduler.running,
        "database": "ok"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5200)