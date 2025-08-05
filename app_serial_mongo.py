from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response, session
from flask_socketio import SocketIO
from flask_pymongo import PyMongo
from datetime import datetime, timedelta
import io
import csv
import requests
import serial
import threading
from pymongo import DESCENDING

app = Flask(__name__)
app.secret_key = 'um_segredo_para_flash'
socketio = SocketIO(app)

app.config["MONGO_URI"] = "mongodb+srv://admin:adminstellantis@cluster0.biopvyg.mongodb.net/ControlePista?retryWrites=true&w=majority&appName=Cluster0"
mongo = PyMongo(app)


@app.route('/', methods=['GET'])
def index():
    veiculos_na_pista = list(mongo.db.access_log.find(
        {
            "exit_time": None,
            "tag_id": {"$ne": None}
        },
        {
            "_id": 0,
            "tag_id": 1,
            "user_name_snapshot": 1,
            "chassi_snapshot": 1,
            "modelo_snapshot": 1,
            "entry_time": 1
        }
    ).sort("entry_time", 1))

    total_veiculos = len(veiculos_na_pista)
    return render_template('pista.html', veiculos=veiculos_na_pista, total=total_veiculos)

@app.route('/access', methods=['POST'])
def access():
    data = request.get_json(force=True)
    tag = data.get('tag')
    is_proximity_alert = data.get('alert', False)

    # --- 0) Debounce: ignora posts muito próximos da mesma tag ---
    if tag:
        ultimo = mongo.db.access_log.find_one(
            {"tag_id": tag},
            sort=[("entry_time", DESCENDING)]
        )
        if ultimo:
            delta = datetime.now() - ultimo["entry_time"]
            if delta < timedelta(seconds=2):
                # Ignora duplicata rápida
                return jsonify({"status": "duplicate_ignored"}), 200

    # --- 1) Alerta de proximidade (sem tag) ---
    if is_proximity_alert:
        message = 'ALERTA: Aproximação detectada sem leitura de TAG!'
        category = 'warning'
        socketio.emit('alerta', {'message': message, 'category': category})

        mongo.db.access_log.insert_one({
            "entry_time": datetime.now(),
            "alert": True,
            "details": "Alerta de proximidade sem tag"
        })
        return jsonify({"status": "alert_triggered"}), 200

    # --- 2) Verifica se a tag está cadastrada ---
    tag_info = mongo.db.tags.find_one({"tag_id": tag})
    if not tag_info:
        message = f'ALERTA: Tentativa de acesso com TAG desconhecida ({tag})!'
        category = 'danger'
        socketio.emit('alerta', {'message': message, 'category': category})
       
        return jsonify({"status": "tag_desconhecida"}), 200

    # --- 3) Verifica se já existe uma entrada ativa (sem exit_time) ---
    entrada_ativa = mongo.db.access_log.find_one({
        "tag_id": tag,
        "exit_time": {"$exists": False}
    })
    if entrada_ativa:
        user_name = tag_info.get('user_name', tag)
        message = f"ALERTA: O usuário '{user_name}' já se encontra na pista!"
        category = 'warning'
        socketio.emit('alerta', {'message': message, 'category': category})
        return jsonify({"status": "ja_na_pista"}), 200

    # --- 4) Decide permissão e registra entrada ---
    user_name_ss = tag_info.get("user_name", "Desconhecido")
    email_ss     = tag_info.get("email", "")
    chassi_ss    = tag_info.get("chassi", "")
    modelo_ss    = tag_info.get("modelo", "")
    access_granted = tag_info.get("has_access", False)

    if not access_granted:
        message    = f'ACESSO NEGADO: {user_name_ss} ({tag}) não tem permissão!'
        category   = 'danger'
        alert_flag = True
    else:
        message    = f"Acesso liberado: '{user_name_ss}' entrou na pista."
        category   = 'success'
        alert_flag = False

    # Emite alerta no front
    socketio.emit('alerta', {'message': message, 'category': category})

    # Registra no histórico
    mongo.db.access_log.insert_one({
        "tag_id": tag,
        "entry_time": datetime.now(),
        "alert": alert_flag,
        "details": message,
        "user_name_snapshot": user_name_ss,
        "email_snapshot": email_ss,
        "chassi_snapshot": chassi_ss,
        "modelo_snapshot": modelo_ss
    })

    # Atualiza lista de veículos se acesso foi liberado
    if not alert_flag:
        socketio.emit('veiculo_atualizado')

    return jsonify({"status": "ok"}),


@app.route('/registrar_saida/<tag_id>', methods=['POST'])
def registrar_saida(tag_id):
    try:
        result = mongo.db.access_log.update_one(
            {
                "tag_id": tag_id,
                "exit_time": {"$exists": False}
            },
            {
                "$set": {"exit_time": datetime.now()}
            }
        )

        if result.modified_count == 0:
            return jsonify({"error": "Nenhuma entrada ativa encontrada para esta tag."}), 404

        socketio.emit('veiculo_atualizado')
        return jsonify({"status": "ok", "message": "Saída registrada com sucesso!"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/scan', methods=['POST'])
def scan_tag():
    data = request.get_json()
    if data and 'tag_id' in data:
        socketio.emit('nova_tag_escaneada', {'tag_id': data['tag_id']})
        return jsonify({"status": "ok"}), 200
    return jsonify({"error": "Nenhuma tag_id fornecida"}), 400


@app.route('/historico', methods=['GET'])
def historico():
    logs = list(mongo.db.access_log.find(
        {"tag_id": {"$ne": None}},
        {
            "_id": 0,
            "tag_id": 1,
            "user_name_snapshot": 1,
            "email_snapshot": 1,
            "chassi_snapshot": 1,
            "modelo_snapshot": 1,
            "entry_time": 1,
            "exit_time": 1,
            "alert": 1
        }
    ).sort("entry_time", -1))

    return render_template('historico.html', logs=logs)


@app.route('/gerenciar', methods=['GET'])
def gerenciar_tags():
    tags = list(mongo.db.tags.find({}, {
        "_id": 0,
        "tag_id": 1,
        "user_name": 1,
        "email": 1,
        "chassi": 1,
        "modelo": 1,
        "has_access": 1
    }))

    for tag in tags:
        entrada_ativa = mongo.db.access_log.find_one({
            "tag_id": tag["tag_id"],
            "exit_time": {"$exists": False}
        })
        tag["status"] = "Na Pista" if entrada_ativa else "Disponível"

    tags.sort(key=lambda t: (t["status"] != "Na Pista", t.get("user_name", "").lower()))
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

    try:
        existente = mongo.db.tags.find_one({"tag_id": tag_id})
        if existente:
            flash(f"Erro: A Tag ID '{tag_id}' já está cadastrada.", 'danger')
            return redirect(url_for('form_cadastro'))

        mongo.db.tags.insert_one({
            "tag_id": tag_id,
            "user_name": user_name,
            "email": email,
            "chassi": chassi,
            "modelo": modelo,
            "has_access": has_access
        })

        flash(f"Tag '{tag_id}' cadastrada com sucesso!", 'success')
        return redirect(url_for('gerenciar_tags'))

    except Exception as e:
        flash(f"Ocorreu um erro ao cadastrar a tag: {str(e)}", 'danger')
        return redirect(url_for('form_cadastro'))


@app.route('/editar/<tag_id>', methods=['GET'])
def form_editar_tag(tag_id):
    tag = mongo.db.tags.find_one({"tag_id": tag_id}, {"_id": 0})
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

    try:
        result = mongo.db.tags.update_one(
            {"tag_id": tag_id},
            {
                "$set": {
                    "user_name": user_name,
                    "email": email,
                    "chassi": chassi,
                    "modelo": modelo,
                    "has_access": has_access
                }
            }
        )

        if result.matched_count == 0:
            flash(f"Tag '{tag_id}' não encontrada.", 'danger')
        else:
            flash(f"Tag '{tag_id}' atualizada com sucesso!", 'success')

    except Exception as e:
        flash(f"Erro ao atualizar a tag: {str(e)}", 'danger')

    return redirect(url_for('gerenciar_tags'))


@app.route('/exportar')
def exportar_csv():
    try:
        dias = request.args.get('dias', default=7, type=int)
    except (ValueError, TypeError):
        dias = 7

    data_inicio = datetime.now() - timedelta(days=dias)

    logs = list(mongo.db.access_log.find(
        {
            "entry_time": {"$gte": data_inicio},
            "tag_id": {"$ne": None}
        },
        {
            "_id": 0,
            "tag_id": 1,
            "user_name_snapshot": 1,
            "email_snapshot": 1,
            "chassi_snapshot": 1,
            "modelo_snapshot": 1,
            "entry_time": 1,
            "exit_time": 1,
            "alert": 1
        }
    ).sort("entry_time", -1))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Tag ID', 'Usuario', 'Email', 'Chassi', 'Modelo', 'Entrada', 'Saida', 'Alerta'])
    for log in logs:
        writer.writerow([
            log['tag_id'],
            log.get('user_name_snapshot', ''),
            log.get('email_snapshot', ''),
            log.get('chassi_snapshot', ''),
            log.get('modelo_snapshot', ''),
            log['entry_time'].strftime('%d/%m/%Y %H:%M:%S') if log.get('entry_time') else '',
            log['exit_time'].strftime('%d/%m/%Y %H:%M:%S') if log.get('exit_time') else '',
            'Sim' if log.get('alert') else 'Nao'
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment;filename=relatorio_acesso_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return response


def serial_listener():
    try:
        ser = serial.Serial('COM6', 9600, timeout=1)
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
    t_serial = threading.Thread(target=serial_listener, daemon=True)
    t_serial.start()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=False)
