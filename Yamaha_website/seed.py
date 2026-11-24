from app import create_app, db
from app.models import User, Machine, Report, Issue
app=create_app()
with app.app_context():
    if not User.query.filter_by(employee_id='MGR001').first():
        manager=User(employee_id='MGR001',name='Factory Manager',role='manager'); manager.set_password('Manager@123'); db.session.add(manager)
    workers=[]
    for eid,name in [('WRK001','Ravi'),('WRK002','Kumar'),('WRK003','Anu')]:
        u=User.query.filter_by(employee_id=eid).first()
        if not u:
            u=User(employee_id=eid,name=name,role='worker'); u.set_password('Worker@123'); db.session.add(u)
        workers.append(u)
    machines=[('WELD-01','Welding Robot','Welding'),('PAINT-01','Paint Booth','Painting'),('CNC-01','CNC Machine','Machining'),('ENG-01','Engine Assembly Line','Assembly'),('PRESS-01','Frame Press','Fabrication')]
    for code,name,dept in machines:
        if not Machine.query.filter_by(machine_code=code).first(): db.session.add(Machine(machine_code=code,name=name,department=dept))
    db.session.commit()
    print('Seed complete. Manager: MGR001 / Manager@123; Workers: WRK001, WRK002, WRK003 / Worker@123')
