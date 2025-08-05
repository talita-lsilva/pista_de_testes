from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response, session
from flask_socketio import SocketIO
import mysql.connector
from datetime import datetime, timedelta
import io
import csv
import requests
import serial
import threading

app = Flask(__name__)
app.secret_key = 'um_segredo_para_flash'
socketio = SocketIO(app)

# Configuração do banco de dados local
db_conf = {
    'host': 'localhost',
    'user': 'appuser',
    'password': 'Galodoido13',
    'database': 'controle_pista'
}

def get_db_connection():
    return mysql.connector.connect(**db_conf)

@app.route('/', methods=['GET'])
def index():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT tag_id, user_name_snapshot, chassi_snapshot, modelo_snapshot, entry_time
        FROM access_log
        WHERE exit_time IS NULL AND tag_id IS NOT NULL
        ORDER BY entry_time ASC
    """)
    veiculos_na_pista = cursor.fetchall()
    total_veiculos = len(veiculos_na_pista)
    cursor.close()
    conn.close()
    return render_template('pista.html', veiculos=veiculos_na_pista, total=total_veiculos)

@app.route('/access', methods=['POST'])
def access():
    data = request.get_json(force=True)
    tag = data.get('tag')
    is_proximity_alert = data.get('alert', False)
    conn = None

    try:
        if is_proximity_alert:
            message = 'ALERTA: Aproximação detectada sem leitura de TAG!'
            category = 'warning'
            socketio.emit('alerta', {'message': message, 'category': category})
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO access_log (entry_time, alert, details) VALUES (%s, %s, %s)",
                (datetime.now(), True, "Alerta de proximidade sem tag")
            )
            conn.commit()
            return jsonify({"status": "alert_triggered"}), 200

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT log_id, user_name_snapshot FROM access_log WHERE tag_id = %s AND exit_time IS NULL", (tag,))
        active_entry = cursor.fetchone()
        if active_entry:
            user_name = active_entry['user_name_snapshot'] or tag
            message = f"ALERTA: O usuário '{user_name}' já se encontra na pista!"
            socketio.emit('alerta', {'message': message, 'category': 'warning'})
            return jsonify({"error": "duplicate_entry"}), 409

        cursor.execute("SELECT * FROM tags WHERE tag_id = %s", (tag,))
        tag_info = cursor.fetchone()

        if not tag_info:
            message = f'ALERTA: Tentativa de acesso com TAG desconhecida ({tag})!'
            category = 'danger'
            alert_flag = True
            user_name_ss = 'Desconhecido'
            email_ss = ''
            chassi_ss = ''
            modelo_ss = ''

            socketio.emit('alerta', {'message': message, 'category': category})

            try:
                cursor.execute(
                    """INSERT INTO access_log (tag_id, entry_time, alert, details, user_name_snapshot, email_snapshot, chassi_snapshot, modelo_snapshot) 
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (tag, datetime.now(), alert_flag, message, user_name_ss, email_ss, chassi_ss, modelo_ss)
                )
                conn.commit()
            except mysql.connector.Error as insert_error:
                app.logger.exception("Erro ao registrar acesso de tag desconhecida")
                return jsonify({"error": "Erro ao registrar acesso de tag desconhecida"}), 500

            return jsonify({"status": "tag_desconhecida"}), 200

        user_name_ss, email_ss, chassi_ss, modelo_ss = (
            tag_info['user_name'], tag_info['email'], tag_info['chassi'], tag_info['modelo']
        )
        access_granted = bool(tag_info['has_access'])

        if not access_granted:
            message = f'ACESSO NEGADO: {user_name_ss} ({tag}) não tem permissão!'
            category = 'danger'
            alert_flag = True
        else:
            message = f"Acesso liberado: '{user_name_ss}' entrou na pista."
            category = 'success'
            alert_flag = False

        socketio.emit('alerta', {'message': message, 'category': category})

        cursor.execute(
            """INSERT INTO access_log (tag_id, entry_time, alert, details, user_name_snapshot, email_snapshot, chassi_snapshot, modelo_snapshot) 
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (tag, datetime.now(), alert_flag, message, user_name_ss, email_ss, chassi_ss, modelo_ss)
        )
        conn.commit()

        if not alert_flag:
            socketio.emit('veiculo_atualizado')

        return jsonify({"status": "ok"}), 200

    except mysql.connector.Error as e:
        if conn: conn.rollback()
        app.logger.exception("Erro na rota /access")
        socketio.emit('alerta', {'message': 'Erro interno no servidor ao processar acesso.', 'category': 'danger'})
        return jsonify({"error": str(e)}), 500
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

@app.route('/registrar_saida/<tag_id>', methods=['POST'])
def registrar_saida(tag_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT log_id FROM access_log WHERE tag_id = %s AND exit_time IS NULL ORDER BY entry_time DESC LIMIT 1",
            (tag_id,)
        )
        row = cursor.fetchone()

        if row:
            log_id = row[0]
            cursor.execute("UPDATE access_log SET exit_time = %s WHERE log_id = %s", (datetime.now(), log_id))
            conn.commit()
            socketio.emit('veiculo_atualizado')
            return jsonify({"status": "ok", "message": "Saída registrada com sucesso!"}), 200
        else:
            return jsonify({"error": "Nenhuma entrada ativa encontrada para esta tag."}), 404

    except mysql.connector.Error as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

@app.route('/scan', methods=['POST'])
def scan_tag():
    data = request.get_json()
    if data and 'tag_id' in data:
        tag_id = data['tag_id']
        socketio.emit('nova_tag_escaneada', {'tag_id': tag_id})
        return jsonify({"status": "ok"}), 200
    return jsonify({"error": "Nenhuma tag_id fornecida"}), 400

@app.route('/historico', methods=['GET'])
def historico():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT tag_id, user_name_snapshot, email_snapshot, chassi_snapshot, 
               modelo_snapshot, entry_time, exit_time, alert
        FROM access_log
        WHERE tag_id IS NOT NULL
        ORDER BY entry_time DESC
    """)
    logs = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('historico.html', logs=logs)

@app.route('/gerenciar', methods=['GET'])
def gerenciar_tags(): 
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT t.tag_id, t.user_name, t.email, t.chassi, t.modelo, t.has_access,
            (CASE WHEN EXISTS (
                SELECT 1 FROM access_log al WHERE al.tag_id = t.tag_id AND al.exit_time IS NULL
            )
            THEN 'Na Pista'
            ELSE 'Disponível'
            END) AS status
        FROM tags t
        ORDER BY status DESC, t.user_name ASC
    """)
    tags = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('cadastro_tag.html', tags=tags)

@app.route('/cadastro', methods=['GET'])
def form_cadastro():
    tag_id_preencher = request.args.get('tag_id', '') 
    return render_template('cadastro.html', tag_id_preencher=tag_id_preencher)

@app.route('/cadastro_tag', methods=['POST'])
def cadastro_tag():
    tag_id = request.form.get('tag_id', '').strip().upper()
    user_name = request.form.get('user_name')
    email = request.form.get('email')
    chassi = request.form.get('chassi')
    modelo = request.form.get('modelo')
    has_access = 'has_access' in request.form
    conn, cursor = None, None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tags (tag_id, user_name, email, chassi, modelo, has_access) VALUES (%s, %s, %s, %s, %s, %s)",
                       (tag_id, user_name, email, chassi, modelo, has_access))
        conn.commit()
        flash(f"Tag '{tag_id}' cadastrada com sucesso!", 'success')
        return redirect(url_for('gerenciar_tags'))
    except mysql.connector.Error as e:
        if e.errno == 1062:
            flash(f"Erro: A Tag ID '{tag_id}' já está cadastrada.", 'danger')
        else:
            flash(f"Ocorreu um erro no banco de dados: {e.msg}", 'danger')
        conn.rollback()
        return redirect(url_for('form_cadastro'))
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

@app.route('/editar/<tag_id>', methods=['GET'])
def form_editar_tag(tag_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM tags WHERE tag_id = %s", (tag_id,))
    tag = cursor.fetchone()
    cursor.close()
    conn.close()
    if not tag:
        flash("Tag não encontrada!", "danger")
        return redirect(url_for('gerenciar_tags'))
    return render_template('editar_tag.html', tag=tag)

@app.route('/atualizar/<tag_id>', methods=['POST'])
def atualizar_tag(tag_id):
    user_name = request.form.get('user_name')
    email = request.form.get('email')
    chassi = request.form.get('chassi')
    modelo = request.form.get('modelo')
    has_access = 'has_access' in request.form
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE tags SET user_name = %s, email = %s, chassi = %s, modelo = %s, has_access = %s WHERE tag_id = %s",
            (user_name, email, chassi, modelo, has_access, tag_id)
        )
        conn.commit()
        flash(f"Tag '{tag_id}' atualizada com sucesso!", 'success')
    except mysql.connector.Error as e:
        conn.rollback()
        flash(f"Erro ao atualizar a tag: {e.msg}", 'danger')
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('gerenciar_tags'))

@app.route('/exportar')
def exportar_csv():
    try:
        dias = request.args.get('dias', default=7, type=int)
    except (ValueError, TypeError):
        dias = 7
    data_inicio = datetime.now() - timedelta(days=dias)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT * FROM access_log WHERE entry_time >= %s AND tag_id IS NOT NULL ORDER BY entry_time DESC
    """, (data_inicio,))
    logs = cursor.fetchall()
    cursor.close()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Tag ID', 'Usuario', 'Email', 'Chassi', 'Modelo', 'Entrada', 'Saida', 'Alerta'])
    for log in logs:
        writer.writerow([
            log['tag_id'], log['user_name_snapshot'], log['email_snapshot'], log['chassi_snapshot'],
            log['modelo_snapshot'],
            log['entry_time'].strftime('%d/%m/%Y %H:%M:%S') if log['entry_time'] else '',
            log['exit_time'].strftime('%d/%m/%Y %H:%M:%S') if log['exit_time'] else '',
            'Sim' if log['alert'] else 'Nao'
        ])
    
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment;filename=relatorio_acesso_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return response

def serial_listener():
    try:
        ser = serial.Serial('COM9', 9600, timeout=1)
        print("Serial iniciado. Aguardando dados...")

        while True:
            if ser.in_waiting > 0:
                try:
                    linha = ser.readline().decode('utf-8', errors='ignore').strip()
                    print(f"Serial: {linha}")

                    if linha.startswith("TAG:"):
                        tag = linha[4:]
                        print(f"Enviando TAG: {tag} para /access")
                        requests.post("http://localhost:5000/access", json={"tag": tag})
                    
                    elif linha == "ALERTA":
                        print("Enviando ALERTA para /access")
                        requests.post("http://localhost:5000/access", json={"tag": "00000000", "alert": True})
                except Exception as e:
                    print(f"Erro ao processar linha da serial: {e}")

    except serial.SerialException as e:
        print(f"Erro na porta serial: {e}")


if __name__ == '__main__':
    # Inicia a leitura da porta serial em segundo plano
    t_serial = threading.Thread(target=serial_listener, daemon=True)
    t_serial.start()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=False)

