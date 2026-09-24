"""
community.py
============
Route zote za Community (groups + pages).

Unganisha kwenye app.py:
    from community import register_community_routes
    register_community_routes(app)
"""

from flask import session, redirect, url_for, render_template, request, flash


def register_community_routes(app):
    """Sajili route za community. Endpoint names zinabaki sawa → templates hazibadiliki."""
    from app import get_db_connection

    # ====================== COMMUNITY ======================

    @app.route('/community')
    def community():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('community/community.html')

    @app.route('/community/university')
    @app.route('/community/university/<sub>')
    def community_university(sub='all'):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('community/university.html', sub=sub)

    @app.route('/community/gamers')
    @app.route('/community/gamers/<sub>')
    def community_gamers(sub='all'):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('community/gamers.html', sub=sub)

    @app.route('/community/politics')
    def community_politics():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('community/politics.html')

    @app.route('/community/agriculture-business')
    def community_agriculture():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('community/agriculture_business.html')

    @app.route('/community/for-you')
    def community_for_you():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('community/for_you.html')

    @app.route('/community/friend-zone')
    def community_friend_zone():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        q = request.args.get('q', '').strip()
        results = []
        if q:
            conn = get_db_connection()
            results = conn.execute(
                'SELECT id, username, full_name, profile_pic, is_verified, bio FROM users WHERE username LIKE ? AND id != ? LIMIT 30',
                (f'%{q}%', session['user_id'])
            ).fetchall()
            conn.close()
        return render_template('community/friend_zone.html', query=q, results=results)

    @app.route('/community/find-group')
    def community_find_group():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        q = request.args.get('q', '').strip()
        conn = get_db_connection()
        if q:
            groups = conn.execute('''
                SELECT g.*, u.username AS creator_name,
                       (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) AS members_count
                FROM community_groups g
                JOIN users u ON g.creator_id = u.id
                WHERE g.name LIKE ? OR g.description LIKE ?
                ORDER BY g.id DESC
            ''', (f'%{q}%', f'%{q}%')).fetchall()
        else:
            groups = conn.execute('''
                SELECT g.*, u.username AS creator_name,
                       (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) AS members_count
                FROM community_groups g
                JOIN users u ON g.creator_id = u.id
                ORDER BY g.id DESC
            ''').fetchall()
        conn.close()
        return render_template('community/find_group.html', groups=groups, query=q)

    @app.route('/community/create-group', methods=['GET', 'POST'])
    def community_create_group():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip()
            category = request.form.get('category', 'general')
            if not name:
                flash('Jina la group linahitajika')
                return redirect(url_for('community_create_group'))
            conn = get_db_connection()
            cur = conn.execute(
                'INSERT INTO community_groups (name, description, category, creator_id) VALUES (?, ?, ?, ?)',
                (name, description, category, session['user_id'])
            )
            group_id = cur.lastrowid
            conn.execute(
                'INSERT INTO group_members (group_id, user_id) VALUES (?, ?)',
                (group_id, session['user_id'])
            )
            conn.commit()
            conn.close()
            flash('Group limeundwa!')
            return redirect(url_for('community_group', group_id=group_id))
        return render_template('community/create_group.html')

    @app.route('/community/group/<int:group_id>', methods=['GET', 'POST'])
    def community_group(group_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        conn = get_db_connection()
        group = conn.execute('''
            SELECT g.*, u.username AS creator_name,
                   (SELECT COUNT(*) FROM group_members WHERE group_id = g.id) AS members_count
            FROM community_groups g
            JOIN users u ON g.creator_id = u.id
            WHERE g.id = ?
        ''', (group_id,)).fetchone()
        if not group:
            conn.close()
            flash('Group halipatikani')
            return redirect(url_for('community_find_group'))

        is_member = conn.execute(
            'SELECT id FROM group_members WHERE group_id = ? AND user_id = ?',
            (group_id, session['user_id'])
        ).fetchone()

        if request.method == 'POST' and is_member:
            msg = request.form.get('message', '').strip()
            if msg:
                conn.execute(
                    'INSERT INTO group_messages (group_id, user_id, message) VALUES (?, ?, ?)',
                    (group_id, session['user_id'], msg)
                )
                conn.commit()

        messages = conn.execute('''
            SELECT m.*, u.username, u.profile_pic
            FROM group_messages m
            JOIN users u ON m.user_id = u.id
            WHERE m.group_id = ?
            ORDER BY m.id ASC
            LIMIT 100
        ''', (group_id,)).fetchall()
        conn.close()
        return render_template('community/group.html', group=group, messages=messages, is_member=bool(is_member))

    @app.route('/community/join-group/<int:group_id>', methods=['POST'])
    def community_join_group(group_id):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        conn = get_db_connection()
        try:
            conn.execute(
                'INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)',
                (group_id, session['user_id'])
            )
            conn.commit()
            flash('Umejiunga na group!')
        except Exception:
            flash('Imeshindikana')
        finally:
            conn.close()
        return redirect(url_for('community_group', group_id=group_id))


